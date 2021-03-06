from typing import Tuple
import pdb 
import math
import torch
from torch.nn import functional as F


class SourceAttention(torch.nn.Module):
    def __init__(self,
                 language_dim: int,
                 image_dim: int, 
                 output_dim: int):
        super(SourceAttention, self).__init__() 
        self.language_dim = language_dim
        self.image_dim = image_dim
        self.output_dim = output_dim

        self.q_proj = torch.nn.Linear(image_dim, output_dim) 
        self.k_proj = torch.nn.Linear(language_dim, output_dim) 
        self.v_proj = torch.nn.Linear(language_dim, output_dim) 

    def forward(self, q, k, v):
        # flatten q [batch, width, height, hidden] to [batch, width * height , hidden]
        bsz, hidden_dim, width, height = q.shape 
        q = q.reshape(bsz, width * height, hidden_dim) 
        # project keys queries and values 
        q, k, v = self.q_proj(q), self.k_proj(k), self.v_proj(v) 
        # get image to input tokens attention weights, scaled by dim 
        weights = torch.bmm(q, k.permute(0,2,1)) 
        weights = weights/math.sqrt(self.output_dim)

        # softmax the weights
        weights = F.softmax(weights, dim = 2) 
        # reweight values (langauge) by attention weight 
        output  = torch.bmm(weights, v) 
        # break back out to image shape 
        output = output.reshape(bsz, self.output_dim, width, height) 

        return output 

class BaseFusionModule(torch.nn.Module):
    def __init__(self,
                 image_size,
                 language_size):
        super(BaseFusionModule, self).__init__() 

        self.image_size = image_size
        self.language_size = language_size
        self.output_dim = image_size + language_size

    def forward(self, image, language):
        raise NotImplementedError

class ConcatFusionModule(BaseFusionModule): 
    def __init__(self,
                 image_size,
                 language_size):
        super(ConcatFusionModule, self).__init__(image_size, language_size)

    def forward(self, image, language):
        output = torch.cat([image, language], dim=1)
        return output 

class TiledFusionModule(BaseFusionModule):
    def __init__(self,
                 image_size,
                 language_size):
        super(TiledFusionModule, self).__init__(image_size, language_size)
        self.output_dim = self.image_size + self.language_size 

    def forward(self, image, language):
        bsz, n_channels, width, height = image.shape 
        # language: bsz x 2
        __, num_lang_channels = language.shape
        language = language.reshape((bsz, num_lang_channels, 1, 1)) 
        language = language.repeat((1, 1, width, height)) 
        # cat across channel dimension 
        output = torch.cat([image, language], dim=1) 
        return output 


class LanguageEncoder(torch.nn.Module):
    """
    Handle language instructions as an API call to an encoder
    that tokenizes, embed tokens, and runs a selected encoder 
    over it, returning an output specified by the model.
    """
    def __init__(self,
                 image_encoder: torch.nn.Module,
                 embedder: torch.nn.Module,
                 encoder: torch.nn.Module,
                 fuser: BaseFusionModule,
                 output_module: torch.nn.Module,
                 block_prediction_module: torch.nn.Module,
                 device: torch.device,
                 compute_block_dist: bool):
        """
        embedder: a choice of 
        encoder: a choice of LSTM or Transformer 
        output_type: choices are object mask, dense vector, 
        """
        super(LanguageEncoder, self).__init__() 

        self.image_encoder = image_encoder
        self.embedder = embedder
        self.encoder = encoder
        self.fuser = fuser
        self.output_module = output_module
        self.device = device 
        self.block_prediction_module = block_prediction_module
        self.softmax_fxn = torch.nn.LogSoftmax(dim = -1)
        self.compute_block_dist = compute_block_dist

        # enable cuda
        for module in [self.embedder, self.image_encoder, self.encoder, self.fuser, self.output_module, self.block_prediction_module]:
            module = module.to(self.device) 
            module.device = device 

    def forward(self,
                data_batch: dict) -> torch.Tensor: 
        language = data_batch["command"]
        # sort lengths 
        lengths = data_batch["length"]
        lengths = [(i,x) for i, x in enumerate(lengths)]
        lengths = sorted(lengths, key = lambda x: x[1], reverse=True)
        idxs, lengths = zip(*lengths) 
        # tensorize lengths 
        lengths  = torch.tensor(lengths).float() 
        # at train-time, uses the gold previous input 
        pos_input = data_batch["previous_position"]
        # embed langauge 
        if type(language[0]) == str:
            lang_embedded = self.embedder(language).unsqueeze(0).to(self.device) 
        else:
            try:
                lang_embedded = torch.cat([self.embedder(language[i]).unsqueeze(0) for i in idxs], dim=0).to(self.device) 
            except RuntimeError:
                return None

        # encode image 
        pos_encoded = self.image_encoder(pos_input) 
        # encode language 
        lang_encoded = self.encoder(lang_embedded, lengths) 
        bsz, __ = lang_encoded.shape 
        # fuse image and language 
        image_and_language = self.fuser(pos_encoded, lang_encoded)
        # get image output 
        image_output = self.output_module(image_and_language) 

        if self.compute_block_dist:
            # get block output 
            block_output = self.block_prediction_module(image_and_language) 
            output = image_output
            #output = self.filter_image_output(block_output, image_output) 
        else:
            output = image_output
            block_output = None 
            
        to_ret = {"next_position": output,
                  "pred_block_logits": block_output} 

        return to_ret

    def filter_image_output(self, block_output, image_output):
        """
        combing block distribution with per-pixel distribution 

        Parameters
        ----------
        block output: [bsz, num_blocks]
           logits per block
        image_output [bsz, 64, 64, 4, num_blocks]         
           logits per pixel 
        """  
        bsz, num_blocks = block_output.shape
        bsz, num_blocks, width, depth, height = image_output.shape 
        image_output = image_output.permute(0, 4, 2, 3, 1) 
        block_output = block_output.reshape((bsz, 1, 1, 1, num_blocks))
        # tile block softmax across shape  
        block_output = block_output.repeat((1, height, width, depth, 1)) 
        # now in logspace with softmax across blocks dim 
        image_output = self.softmax_fxn(image_output) 
        block_output_ln = self.softmax_fxn(block_output) 
        # multiply probs by adding logprobs  
        output = image_output + block_output_ln
        # reshape output 
        output = output.permute(0, 4, 2, 3, 1) 
        return output 

        
        
        



