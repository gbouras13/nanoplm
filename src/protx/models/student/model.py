import torch.nn as nn
import torch.nn.functional as F
import torch
from safetensors.torch import load_file
from transformers import (
    ModernBertModel,
    ModernBertConfig,
)
from transformers.modeling_outputs import BaseModelOutput
from transformers.models.t5.modeling_t5 import T5LayerNorm
from typing import Iterator, Union, List, Generator, Tuple

from .tokenizer import ProtXTokenizer

class ProtX(nn.Module):
    """Student model for ProtX"""

    def __init__(
        self,
        embed_dim: int,
        num_layers: int,
        num_heads: int,
        mlp_activation: str = "swiglu",
    ):
        super().__init__()

        self.tokenizer = ProtXTokenizer()

        self.config = ModernBertConfig(
            vocab_size=self.tokenizer.vocab_size,
            hidden_size=embed_dim,
            intermediate_size=embed_dim * 2,
            num_hidden_layers=num_layers,
            num_attention_heads=num_heads,
            pad_token_id=self.tokenizer.pad_token_id,
            eos_token_id=self.tokenizer.eos_token_id,
            attention_dropout=0.0,
            mlp_dropout=0.0,
            mlp_bias=False,
            attention_bias=False,
        )

        self.model = ModernBertModel(self.config)

        if mlp_activation.lower() == "swiglu":
            for layer in self.model.layers:
                layer.mlp = ModernBertMLPSwiGLU(self.config)

        self.proj = nn.Linear(embed_dim, 1024, bias=False)
        self.proj_norm = T5LayerNorm(1024)

    def forward(self, input_ids, attention_mask, training_mode = False, teacher_embeddings=None):
        student_out = self.model(input_ids=input_ids, attention_mask=attention_mask)
        
        if training_mode:
            projected_repr = self.proj(student_out.last_hidden_state)  # (batch_size, seq_len, 1024)
            projected_repr = self.proj_norm(projected_repr)
            # training mode
            return BaseModelOutput(
                last_hidden_state=projected_repr,
                hidden_states=student_out.hidden_states,
                attentions=student_out.attentions
            )
        else:
            # Inference mode
            return BaseModelOutput(
                last_hidden_state=student_out.last_hidden_state,
                hidden_states=student_out.hidden_states,
                attentions=student_out.attentions
            )

    @staticmethod
    def load_and_generate_embeddings(
        checkpoint_path: str,
        sequences: Union[List[str], Iterator[str]],
        batch_size: int = 32,
        max_length: int = 512,
        device: str = "cuda",
        per_seq_embeddings: bool = True,  # True for pooled, False for per-token
        mlp_activation: str = "swiglu"
    ) -> Generator[Tuple[str, torch.Tensor], None, None]:
        """
        Load model from checkpoint and generate embeddings for sequences.
        Automatically detects model architecture from checkpoint.
        
        Args:
            checkpoint_path: Path to the model.safetensors file
            sequences: Iterator or list of protein sequences
            batch_size: Number of sequences to process at once
            max_length: Maximum sequence length for tokenization
            device: Device to run inference on
            per_seq_embeddings: If True, return pooled sequence-level embeddings [embed_dim].
                               If False, return per-token embeddings [sequence_length, embed_dim]
            mlp_activation: MLP activation function ("swiglu" or others)
            
        Yields:
            Tuple of (sequence, embedding_tensor) for each input sequence
            - If per_seq_embeddings=True: embedding shape is [embed_dim]
            - If per_seq_embeddings=False: embedding shape is [sequence_length, embed_dim] 
        """
        # Automatically detect model architecture from checkpoint
        try:
            embed_dim, num_layers, num_heads = ProtX.inspect_checkpoint_architecture(checkpoint_path)
            print(f"Detected architecture: embed_dim={embed_dim}, num_layers={num_layers}, num_heads={num_heads}")
        except Exception as e:
            print(f"Error detecting architecture: {e}")
            return
        
        # Create model instance with detected architecture
        model = ProtX(
            embed_dim=embed_dim,
            num_layers=num_layers, 
            num_heads=num_heads,
            mlp_activation=mlp_activation
        )
        
        # Load the checkpoint
        try:
            state_dict = load_file(checkpoint_path)
            model.load_state_dict(state_dict, strict=False)
            print(f"Successfully loaded checkpoint from {checkpoint_path}")
        except Exception as e:
            print(f"Error loading checkpoint: {e}")
            return
        
        # Move model to device and set to eval mode
        model.to(device)
        model.eval()
        
        # Convert sequences to list if it's an iterator
        if not isinstance(sequences, list):
            sequences = list(sequences)
        
        # Process sequences in batches
        with torch.no_grad():
            for i in range(0, len(sequences), batch_size):
                batch_sequences = sequences[i:i + batch_size]
                
                # Tokenize the batch
                tokenized = model.tokenizer.batch_encode_plus(
                    batch_sequences,
                    padding=True,
                    truncation=True,
                    max_length=max_length,
                    return_tensors="pt"
                )
                
                # Move to device
                input_ids = tokenized["input_ids"].to(device)
                attention_mask = tokenized["attention_mask"].to(device)
                
                # Generate embeddings
                outputs = model.forward(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    training_mode=False
                )
                
                # Extract embeddings based on per_seq_embeddings setting
                embeddings = outputs.last_hidden_state  # (batch_size, seq_len, embed_dim)
                
                if per_seq_embeddings:
                    # Return pooled sequence-level embeddings (mean pooling)
                    # Exclude EOS token by removing the last position from embeddings and mask
                    embeddings_no_eos = embeddings[:, :-1, :]  # Remove last token (EOS)
                    mask_no_eos = attention_mask[:, :-1]  # Remove last position from mask
                    
                    mask_expanded = mask_no_eos.unsqueeze(-1).expand(embeddings_no_eos.size()).float()
                    masked_embeddings = embeddings_no_eos * mask_expanded
                    summed = torch.sum(masked_embeddings, dim=1)
                    summed_mask = torch.clamp(mask_expanded.sum(dim=1), min=1e-9)
                    mean_pooled = summed / summed_mask
                    
                    # Yield each sequence with its pooled embedding
                    for j, (seq, embedding) in enumerate(zip(batch_sequences, mean_pooled)):
                        yield seq, embedding.cpu()
                else:
                    # Return per-token embeddings (remove padding and EOS token)
                    for j, (seq, seq_embeddings, seq_mask) in enumerate(zip(batch_sequences, embeddings, attention_mask)):
                        # Get actual sequence length (excluding padding and EOS)
                        actual_length = seq_mask.sum().item() - 1  # Subtract 1 for EOS token
                        yield seq, seq_embeddings[:actual_length].cpu()
    
    @staticmethod
    def inspect_checkpoint_architecture(checkpoint_path: str) -> Tuple[int, int, int]:
        """
        Inspect model architecture from safetensors checkpoint file.
        
        Args:
            checkpoint_path: Path to the model.safetensors file
            
        Returns:
            Tuple of (embed_dim, num_layers, num_heads)
            
        Raises:
            ValueError: If architecture cannot be determined from checkpoint
            FileNotFoundError: If checkpoint file doesn't exist
        """
        try:
            # Load the state dict
            state_dict = load_file(checkpoint_path)
        except Exception as e:
            raise FileNotFoundError(f"Error loading checkpoint from {checkpoint_path}: {e}")
        
        # Extract architecture information from parameter shapes
        embed_dim = None
        num_layers = 0
        num_heads = None
        
        # Find embed_dim from various possible parameters
        for key, tensor in state_dict.items():
            if 'embeddings.tok_embeddings.weight' in key:
                vocab_size, embed_dim = tensor.shape
                break
            elif 'model.layers.0.attn.Wo.weight' in key:
                embed_dim, _ = tensor.shape
                break
            elif 'model.layers.0.mlp.Wi.weight' in key:
                _, embed_dim = tensor.shape
                break
        
        if embed_dim is None:
            raise ValueError("Could not determine embed_dim from checkpoint")
        
        # Count number of layers by looking for layer-specific parameters
        layer_indices = set()
        for key in state_dict.keys():
            if 'model.layers.' in key:
                # Extract layer number from key like "model.layers.0.attn.Wo.weight"
                parts = key.split('.')
                for i, part in enumerate(parts):
                    if part == 'layers' and i + 1 < len(parts):
                        try:
                            layer_idx = int(parts[i + 1])
                            layer_indices.add(layer_idx)
                        except ValueError:
                            continue
        
        num_layers = len(layer_indices)
        
        if num_layers == 0:
            raise ValueError("Could not determine num_layers from checkpoint")
        
        # Find number of attention heads from Wqkv matrix
        for key, tensor in state_dict.items():
            if 'model.layers.0.attn.Wqkv.weight' in key:
                # Wqkv weight shape is [3 * embed_dim, embed_dim] for combined Q,K,V
                qkv_dim, model_dim = tensor.shape
                
                if qkv_dim == 3 * embed_dim:
                    # Standard multi-head attention patterns
                    if embed_dim % 64 == 0:
                        num_heads = embed_dim // 64  # head_dim = 64
                    elif embed_dim % 32 == 0:
                        num_heads = embed_dim // 32  # head_dim = 32
                    elif embed_dim % 128 == 0:
                        num_heads = embed_dim // 128  # head_dim = 128
                    else:
                        # Try common head counts
                        for possible_heads in [8, 12, 16, 20, 24, 32]:
                            if embed_dim % possible_heads == 0:
                                num_heads = possible_heads
                                break
                break
        
        # Alternative: look for separate Q,K,V or other attention patterns
        if num_heads is None:
            for key, tensor in state_dict.items():
                if 'query' in key.lower() and 'weight' in key and 'layers.0' in key:
                    # If we find separate query weights, analyze them
                    if len(tensor.shape) == 2:
                        out_dim, in_dim = tensor.shape
                        if in_dim == embed_dim:
                            # num_heads = out_dim / head_dim, try standard head dimensions
                            for head_dim in [32, 64, 128]:
                                if out_dim % head_dim == 0:
                                    num_heads = out_dim // head_dim
                                    break
                    break
        
        if num_heads is None:
            raise ValueError("Could not determine num_heads from checkpoint")
        
        return embed_dim, num_layers, num_heads


class SwiGLU(nn.Module):
    def forward(self, x, gate):
        return F.silu(gate) * x


class ModernBertMLPSwiGLU(nn.Module):
    """Replacement MLP that applies SwiGLU to each ModernBERT layer."""

    def __init__(self, config: ModernBertConfig):
        super().__init__()
        self.Wi = nn.Linear(config.hidden_size, config.intermediate_size * 2, bias=config.mlp_bias)
        self.drop = nn.Dropout(config.mlp_dropout)
        self.act = SwiGLU()
        self.Wo = nn.Linear(config.intermediate_size, config.hidden_size, bias=config.mlp_bias)

    def forward(self, hidden_states):
        x, gate = self.Wi(hidden_states).chunk(2, dim=-1)
        return self.Wo(self.drop(self.act(x, gate)))
