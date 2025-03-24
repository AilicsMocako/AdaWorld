import os
from typing import List

import imageio
import numpy as np
import torch
import torchvision
from PIL import Image
from einops import rearrange
from omegaconf import OmegaConf
from safetensors.torch import load_file as load_safetensors

from vwm.modules.diffusionmodules.sampling import EulerEDMSampler
from vwm.util import instantiate_from_config


def init_model(config, ckpt, load_ckpt: bool = True):
    config = OmegaConf.load(config)
    model = load_model_from_config(config, ckpt if load_ckpt else None)
    return model


def load_model_from_config(config, ckpt: str = None):
    model = instantiate_from_config(config.model)

    if ckpt is not None:
        print(f"Loading model from {ckpt}")
        if ckpt.endswith("ckpt"):
            sd = torch.load(ckpt, map_location="cpu")["state_dict"]
        elif ckpt.endswith("bin"):  # For deepspeed merged checkpoints
            sd = torch.load(ckpt, map_location="cpu")
            for k in list(sd.keys()):  # Remove the prefix
                if "_forward_module" in k:
                    sd[k.replace("_forward_module.", "")] = sd[k]
                del sd[k]
        elif ckpt.endswith("safetensors"):
            sd = load_safetensors(ckpt)
        else:
            raise NotImplementedError
        missing, unexpected = model.load_state_dict(sd, strict=False)
        if len(missing) > 0:
            print(f"Missing keys: {missing}")
        if len(unexpected) > 0:
            print(f"Unexpected keys: {unexpected}")

    model = model.cuda()
    model.eval()
    return model


def perform_save_locally(save_path, samples, mode, n_rows: int = 5):
    save_path = os.path.join(save_path, mode)
    os.makedirs(save_path, exist_ok=True)
    base_count = len(os.listdir(save_path))
    samples = samples.cpu()

    if mode == "images":
        for sample_id, sample in enumerate(samples):
            sample = 255.0 * rearrange(sample.numpy(), "c h w -> h w c")
            Image.fromarray(sample.astype(np.uint8)).save(
                os.path.join(save_path, f"{base_count:04}_{sample_id:02}.png")
            )
    elif mode == "grids":
        grid = torchvision.utils.make_grid(samples, nrow=n_rows)
        grid = 255.0 * grid.transpose(0, 1).transpose(1, 2).squeeze(-1).numpy()
        Image.fromarray(grid.astype(np.uint8)).save(
            os.path.join(save_path, f"{base_count:04}.png")
        )
    elif mode == "videos":
        img_seq = 255.0 * rearrange(samples.numpy(), "t c h w -> t h w c")
        video_save_path = os.path.join(save_path, f"{base_count:04}.mp4")
        writer = imageio.get_writer(video_save_path, fps=5)
        for img in img_seq.astype(np.uint8):
            writer.append_data(img)
        writer.close()
    else:
        raise ValueError(f"Unknown mode {mode}")


def get_guider(guider, scale):
    if guider == "IdentityGuider":
        guider_config = {
            "target": "vwm.modules.diffusionmodules.guiders.IdentityGuider"
        }
    elif guider == "VanillaCFG":
        guider_config = {
            "target": "vwm.modules.diffusionmodules.guiders.VanillaCFG",
            "params": {
                "scale": scale
            }
        }
    else:
        raise ValueError(f"Unknown guider {guider}")
    return guider_config


def init_sampling(
        steps: int = 40,
        sampler: str = "EulerEDMSampler",
        discretization: str = "EDMShiftDiscretization",
        guider: str = "VanillaCFG",
        cfg_scale: float = 7.5,
        n_context_frames: int = 5
):
    discretization_config = get_discretization(discretization)
    guider_config = get_guider(guider, cfg_scale)
    sampler = get_sampler(sampler, steps, discretization_config, guider_config, n_context_frames)
    return sampler


def get_discretization(discretization):
    if discretization == "EDMShiftDiscretization":
        discretization_config = {
            "target": "vwm.modules.diffusionmodules.discretizer.EDMShiftDiscretization"
        }
    else:
        raise ValueError(f"Unknown discretization {discretization}")
    return discretization_config


def get_sampler(sampler, steps, discretization_config, guider_config, n_context_frames):
    if sampler == "EulerEDMSampler":
        s_churn = 0.0
        s_tmin = 0.0
        s_tmax = 999.0
        s_noise = 1.0

        sampler = EulerEDMSampler(
            num_steps=steps,
            discretization_config=discretization_config,
            guider_config=guider_config,
            s_churn=s_churn,
            s_tmin=s_tmin,
            s_tmax=s_tmax,
            s_noise=s_noise,
            verbose=False,
            n_context_frames=n_context_frames
        )
    else:
        raise ValueError(f"Unknown sampler {sampler}")
    return sampler


def do_sample(
        model,
        sampler,
        value_dict,
        input_res: int = 256,
        force_uc_zero_embeddings: List = None
):
    if force_uc_zero_embeddings is None:
        force_uc_zero_embeddings = []
    num_context = len(value_dict["img_seq"]) - 1
    out_length = len(value_dict["gt_frames"]) - 1

    out_samples = torch.zeros(out_length, 3, input_res, input_res).to("cuda")
    z = model.encode_first_stage(value_dict["img_seq"])

    for predict_id in range(out_length):
        batch, batch_uc = get_batch(
            list(set([x.input_key for x in model.conditioner.embedders])),
            value_dict
        )
        c, uc = model.conditioner.get_unconditional_conditioning(
            batch,
            batch_uc=batch_uc,
            force_uc_zero_embeddings=force_uc_zero_embeddings
        )

        randn = torch.randn_like(z)

        def denoiser(input, sigma, c):
            return model.denoiser(model.model, input, sigma, c)

        z_input = z

        samples_z = sampler(denoiser, randn, x_ori=z_input, cond=c, uc=uc)

        samples_x = model.decode_first_stage(samples_z)
        samples = torch.clamp((samples_x + 1.0) / 2.0, min=0.0, max=1.0)

        out_samples[predict_id] = samples[[-1]]
        if predict_id < out_length - 1:
            value_dict["context_len"] = torch.Tensor([min(predict_id + 2, num_context)]).to("cuda")
            source_inputs = value_dict["source_video"][predict_id + 1: predict_id + 3]
            value_dict["lam_inputs"] = torch.cat([source_inputs, samples[[-1]]], dim=0)[None]
            value_dict["cond_frames"] = samples_z[[-1]] / model.scale_factor
            value_dict["cond_frames_without_noise"] = torch.clamp(samples_x[[-1]], min=-1.0, max=1.0)
            for embedder in model.conditioner.embedders:
                if hasattr(embedder, "skip_encode"):
                    embedder.skip_encode = True
            z = torch.cat([samples_z[1:], torch.zeros_like(samples_z[:1])], dim=0)
    for embedder in model.conditioner.embedders:
        if hasattr(embedder, "skip_encode"):
            embedder.skip_encode = False
    return out_samples


def get_batch(keys, value_dict):
    batch = {}
    batch_uc = {}

    for key in keys:
        batch[key] = value_dict[key]
    for key in batch.keys():
        if key not in batch_uc and isinstance(batch[key], torch.Tensor):
            batch_uc[key] = torch.clone(batch[key])
    return batch, batch_uc
