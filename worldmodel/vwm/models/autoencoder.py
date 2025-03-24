import math
from abc import abstractmethod
from contextlib import contextmanager
from typing import Dict, Tuple, Union

import pytorch_lightning as pl
import torch
import torch.nn as nn
from packaging import version
from pytorch_lightning import LightningModule
from vwm.modules.autoencoding.regularizer import AbstractRegularizer
from vwm.modules.ema import LitEma
from vwm.util import get_obj_from_str, instantiate_from_config


class AbstractAutoencoder(LightningModule):
    """
    This is the base class for all autoencoders, including image autoencoders, image autoencoders with discriminators,
    unCLIP models, etc. Hence, it is fairly general, and specific features
    (e.g. discriminator training, encoding, decoding) must be implemented in subclasses.
    """

    def __init__(
            self,
            ema_decay: Union[None, float] = None,
            monitor: Union[None, str] = None,
            input_key: str = "img"
    ):
        super(AbstractAutoencoder, self).__init__()
        self.input_key = input_key
        self.use_ema = ema_decay is not None

        if monitor is not None:
            self.monitor = monitor

        if self.use_ema:
            self.model_ema = LitEma(self, decay=ema_decay)
            print(f"Keeping EMAs of {len(list(self.model_ema.buffers()))}")

        if version.parse(pl.__version__) >= version.parse("2.0.0"):
            self.automatic_optimization = False

    @abstractmethod
    def get_input(self, batch):
        raise NotImplementedError

    def on_train_batch_end(self, *args, **kwargs):
        # For EMA computation
        if self.use_ema:
            self.model_ema(self)

    @contextmanager
    def ema_scope(self, context=None):
        if self.use_ema:
            self.model_ema.store(self.parameters())
            self.model_ema.copy_to(self)
            if context is not None:
                print(f"{context}: Switched to EMA weights")
        try:
            yield None
        finally:
            if self.use_ema:
                self.model_ema.restore(self.parameters())
                if context is not None:
                    print(f"{context}: Restored training weights")

    @abstractmethod
    def encode(self, *args, **kwargs) -> torch.Tensor:
        raise NotImplementedError("encode()-method of abstract base class called")

    @abstractmethod
    def decode(self, *args, **kwargs) -> torch.Tensor:
        raise NotImplementedError("decode()-method of abstract base class called")

    def instantiate_optimizer_from_config(self, params, lr, cfg):
        print(f"Loading >>> {cfg['target']} <<< optimizer from config")
        return get_obj_from_str(cfg["target"])(params, lr=lr, **cfg.get("params", {}))

    def configure_optimizers(self):
        raise NotImplementedError


class AutoencodingEngine(AbstractAutoencoder):
    """
    Base class for all image autoencoders that we train, like VQGAN or AutoencoderKL
    (we also restore them explicitly as special cases for legacy reasons).
    Regularizations such as KL or VQ are moved to the regularizer class.
    """

    def __init__(
            self,
            *args,
            encoder_config: Dict,
            decoder_config: Dict,
            loss_config: Dict,
            regularizer_config: Dict,
            **kwargs
    ):
        super(AutoencodingEngine, self).__init__(*args, **kwargs)
        self.encoder: nn.Module = instantiate_from_config(encoder_config)
        self.decoder: nn.Module = instantiate_from_config(decoder_config)
        self.loss: nn.Module = instantiate_from_config(loss_config)
        self.regularization: AbstractRegularizer = instantiate_from_config(regularizer_config)

    def get_input(self, batch: Dict) -> torch.Tensor:
        # Image tensors should be scaled to -1 ... 1 and in channels-first format (e.g., bchw instead if bhwc)
        return batch[self.input_key]

    def get_last_layer(self):
        return self.decoder.get_last_layer()

    def encode(
            self,
            x: torch.Tensor,
            return_reg_log: bool = False,
            unregularized: bool = False
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, dict]]:
        z = self.encoder(x)
        if unregularized:
            return z, {}
        else:
            z, reg_log = self.regularization(z)
            if return_reg_log:
                return z, reg_log
            else:
                return z

    def decode(self, z: torch.Tensor, **kwargs) -> torch.Tensor:
        x = self.decoder(z, **kwargs)
        return x

    def forward(
            self, x: torch.Tensor, **additional_decode_kwargs
    ) -> Tuple[torch.Tensor, torch.Tensor, Dict]:
        z, reg_log = self.encode(x, return_reg_log=True)
        dec = self.decode(z, **additional_decode_kwargs)
        return z, dec, reg_log


class AutoencodingEngineLegacy(AutoencodingEngine):
    def __init__(self, embed_dim: int, **kwargs):
        self.max_batch_size = kwargs.pop("max_batch_size", None)
        ddconfig = kwargs.pop("ddconfig")
        super(AutoencodingEngineLegacy, self).__init__(
            encoder_config={
                "target": "vwm.modules.diffusionmodules.model.Encoder",
                "params": ddconfig
            },
            decoder_config={
                "target": "vwm.modules.diffusionmodules.model.Decoder",
                "params": ddconfig
            },
            **kwargs
        )
        self.quant_conv = nn.Conv2d(2 * ddconfig["z_channels"], 2 * embed_dim, 1)
        self.post_quant_conv = nn.Conv2d(embed_dim, ddconfig["z_channels"], 1)
        self.embed_dim = embed_dim

    def encode(
            self, x: torch.Tensor, return_reg_log: bool = False
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, dict]]:
        if self.max_batch_size is None:
            z = self.encoder(x)
            z = self.quant_conv(z)
        else:
            N = x.shape[0]
            bs = self.max_batch_size
            n_batches = int(math.ceil(N / bs))
            z = []
            for i_batch in range(n_batches):
                z_batch = self.encoder(x[i_batch * bs: (i_batch + 1) * bs])
                z_batch = self.quant_conv(z_batch)
                z.append(z_batch)
            z = torch.cat(z, 0)

        z, reg_log = self.regularization(z)
        if return_reg_log:
            return z, reg_log
        else:
            return z

    def decode(self, z: torch.Tensor, **decoder_kwargs) -> torch.Tensor:
        if self.max_batch_size is None:
            dec = self.post_quant_conv(z)
            dec = self.decoder(dec, **decoder_kwargs)
        else:
            N = z.shape[0]
            bs = self.max_batch_size
            n_batches = int(math.ceil(N / bs))
            dec = []
            for i_batch in range(n_batches):
                dec_batch = self.post_quant_conv(z[i_batch * bs: (i_batch + 1) * bs])
                dec_batch = self.decoder(dec_batch, **decoder_kwargs)
                dec.append(dec_batch)
            dec = torch.cat(dec, 0)
        return dec


class AutoencoderKL(AutoencodingEngineLegacy):
    def __init__(self, **kwargs):
        super(AutoencoderKL, self).__init__(
            regularizer_config={
                "target": (
                    "vwm.modules.autoencoding.regularizer.DiagonalGaussianRegularizer"
                )
            },
            **kwargs
        )


class AutoencoderKLModeOnly(AutoencodingEngineLegacy):
    def __init__(self, **kwargs):
        super(AutoencoderKLModeOnly, self).__init__(
            regularizer_config={
                "target": (
                    "vwm.modules.autoencoding.regularizer.DiagonalGaussianRegularizer"
                ),
                "params": {"sample": False},
            },
            **kwargs
        )
