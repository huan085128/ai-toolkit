import os
from typing import TYPE_CHECKING, List

import torch
import torchvision
import yaml
from toolkit import train_tools
from toolkit.config_modules import GenerateImageConfig, ModelConfig
from PIL import Image
from toolkit.models.base_model import BaseModel
from diffusers import FluxTransformer2DModel, AutoencoderKL, FluxKontextPipeline
from toolkit.basic import flush
from toolkit.prompt_utils import PromptEmbeds
from toolkit.samplers.custom_flowmatch_sampler import CustomFlowMatchEulerDiscreteScheduler
from toolkit.models.flux import add_model_gpu_splitter_to_flux, bypass_flux_guidance, restore_flux_guidance
from toolkit.dequantize import patch_dequantization_on_save
from toolkit.accelerator import get_accelerator, unwrap_model
from optimum.quanto import freeze, QTensor
from toolkit.util.mask import generate_random_mask, random_dialate_mask
from toolkit.util.quantize import quantize, get_qtype
from transformers import T5TokenizerFast, T5EncoderModel, CLIPTextModel, CLIPTokenizer
from einops import rearrange, repeat
import random
import torch.nn.functional as F

if TYPE_CHECKING:
    from toolkit.data_transfer_object.data_loader import DataLoaderBatchDTO

scheduler_config = {
    "base_image_seq_len": 256,
    "base_shift": 0.5,
    "max_image_seq_len": 4096,
    "max_shift": 1.15,
    "num_train_timesteps": 1000,
    "shift": 3.0,
    "use_dynamic_shifting": True
}



class FluxKontextModel(BaseModel):
    arch = "flux_kontext"

    def __init__(
            self,
            device,
            model_config: ModelConfig,
            dtype='bf16',
            custom_pipeline=None,
            noise_scheduler=None,
            **kwargs
    ):
        super().__init__(
            device,
            model_config,
            dtype,
            custom_pipeline,
            noise_scheduler,
            **kwargs
        )
        self.is_flow_matching = True
        self.is_transformer = True
        self.target_lora_modules = ['FluxTransformer2DModel']

    # static method to get the noise scheduler
    @staticmethod
    def get_train_scheduler():
        return CustomFlowMatchEulerDiscreteScheduler(**scheduler_config)

    def get_bucket_divisibility(self):
        return 16

    def load_model(self):
        dtype = self.torch_dtype
        self.print_and_status_update("Loading Flux Kontext model")
        # will be updated if we detect a existing checkpoint in training folder
        model_path = self.model_config.name_or_path
        # this is the original path put in the model directory
        # it is here because for finetuning we only save the transformer usually
        # so we need this for the VAE, te, etc
        base_model_path = self.model_config.extras_name_or_path

        transformer_path = model_path
        transformer_subfolder = 'transformer'
        if os.path.exists(transformer_path):
            transformer_subfolder = None
            transformer_path = os.path.join(transformer_path, 'transformer')
            # check if the path is a full checkpoint.
            te_folder_path = os.path.join(model_path, 'text_encoder')
            # if we have the te, this folder is a full checkpoint, use it as the base
            if os.path.exists(te_folder_path):
                base_model_path = model_path

        self.print_and_status_update("Loading transformer")
        transformer = FluxTransformer2DModel.from_pretrained(
            transformer_path,
            subfolder=transformer_subfolder,
            torch_dtype=dtype
        )
        transformer.to(self.quantize_device, dtype=dtype)

        if self.model_config.quantize:
            # patch the state dict method
            patch_dequantization_on_save(transformer)
            quantization_type = get_qtype(self.model_config.qtype)
            self.print_and_status_update("Quantizing transformer")
            quantize(transformer, weights=quantization_type,
                     **self.model_config.quantize_kwargs)
            freeze(transformer)
            transformer.to(self.device_torch)
        else:
            transformer.to(self.device_torch, dtype=dtype)

        flush()

        self.print_and_status_update("Loading T5")
        tokenizer_2 = T5TokenizerFast.from_pretrained(
            base_model_path, subfolder="tokenizer_2", torch_dtype=dtype
        )
        text_encoder_2 = T5EncoderModel.from_pretrained(
            base_model_path, subfolder="text_encoder_2", torch_dtype=dtype
        )
        text_encoder_2.to(self.device_torch, dtype=dtype)
        flush()

        if self.model_config.quantize_te:
            self.print_and_status_update("Quantizing T5")
            quantize(text_encoder_2, weights=get_qtype(
                self.model_config.qtype))
            freeze(text_encoder_2)
            flush()

        self.print_and_status_update("Loading CLIP")
        text_encoder = CLIPTextModel.from_pretrained(
            base_model_path, subfolder="text_encoder", torch_dtype=dtype)
        tokenizer = CLIPTokenizer.from_pretrained(
            base_model_path, subfolder="tokenizer", torch_dtype=dtype)
        text_encoder.to(self.device_torch, dtype=dtype)

        self.print_and_status_update("Loading VAE")
        # vae = AutoencoderKL.from_pretrained(
        #     base_model_path, subfolder="vae", torch_dtype=dtype)
        pretrained_vae_model = self.model_config.pretrained_vae_model
        vae = AutoencoderKL.from_pretrained(
            pretrained_vae_model or base_model_path,
            subfolder=None if pretrained_vae_model else "vae",
        )

        self.noise_scheduler = FluxKontextModel.get_train_scheduler()

        self.print_and_status_update("Making pipe")

        pipe: FluxKontextPipeline = FluxKontextPipeline(
            scheduler=self.noise_scheduler,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            text_encoder_2=None,
            tokenizer_2=tokenizer_2,
            vae=vae,
            transformer=None,
        )
        # for quantization, it works best to do these after making the pipe
        pipe.text_encoder_2 = text_encoder_2
        pipe.transformer = transformer

        self.print_and_status_update("Preparing Model")

        text_encoder = [pipe.text_encoder, pipe.text_encoder_2]
        tokenizer = [pipe.tokenizer, pipe.tokenizer_2]

        pipe.transformer = pipe.transformer.to(self.device_torch)

        flush()
        # just to make sure everything is on the right device and dtype
        text_encoder[0].to(self.device_torch)
        text_encoder[0].requires_grad_(False)
        text_encoder[0].eval()
        text_encoder[1].to(self.device_torch)
        text_encoder[1].requires_grad_(False)
        text_encoder[1].eval()
        pipe.transformer = pipe.transformer.to(self.device_torch)
        flush()

        # save it to the model class
        self.vae = vae
        self.text_encoder = text_encoder  # list of text encoders
        self.tokenizer = tokenizer  # list of tokenizers
        self.model = pipe.transformer
        self.pipeline = pipe
        self.print_and_status_update("Model Loaded")

    def get_generation_pipeline(self):
        scheduler = FluxKontextModel.get_train_scheduler()

        pipeline: FluxKontextPipeline = FluxKontextPipeline(
            scheduler=scheduler,
            text_encoder=unwrap_model(self.text_encoder[0]),
            tokenizer=self.tokenizer[0],
            text_encoder_2=unwrap_model(self.text_encoder[1]),
            tokenizer_2=self.tokenizer[1],
            vae=unwrap_model(self.vae),
            transformer=unwrap_model(self.transformer)
        )

        pipeline = pipeline.to(self.device_torch)

        return pipeline

    def generate_single_image(
        self,
        pipeline: FluxKontextPipeline,
        gen_config: GenerateImageConfig,
        conditional_embeds: PromptEmbeds,
        unconditional_embeds: PromptEmbeds,
        generator: torch.Generator,
        extra: dict,
    ):
        if gen_config.ctrl_img is None:
            raise ValueError(
                "Control image is required for Flux Kontext model generation."
            )
        else:
            control_img = Image.open(gen_config.ctrl_img)
            # self.print_and_status_update(f"*****************use_alpha:{gen_config.use_alpha}**************")
            if gen_config.use_alpha:
                # 如果启用alpha通道，则转换为RGBA
                control_img = control_img.convert('RGBA')
            else:
                # 否则，转换为RGB，丢弃alpha通道
                control_img = control_img.convert('RGB')
            # resize to width and height
            if control_img.size != (gen_config.width, gen_config.height):
                control_img = control_img.resize(
                    (gen_config.width, gen_config.height), Image.BILINEAR
                )
        gen_config.width = int(gen_config.width  // 16 * 16)
        gen_config.height = int(gen_config.height // 16 * 16)
        img = pipeline(
            image=control_img,
            prompt_embeds=conditional_embeds.text_embeds,
            pooled_prompt_embeds=conditional_embeds.pooled_embeds,
            height=gen_config.height,
            width=gen_config.width,
            num_inference_steps=gen_config.num_inference_steps,
            guidance_scale=gen_config.guidance_scale,
            latents=gen_config.latents,
            generator=generator,
            max_area=gen_config.height * gen_config.width,
            _auto_resize=False,
            **extra
        ).images[0]
        return img

    def get_noise_prediction(
        self,
        latent_model_input: torch.Tensor,
        timestep: torch.Tensor,  # 0 to 1000 scale
        text_embeddings: PromptEmbeds,
        guidance_embedding_scale: float,
        bypass_guidance_embedding: bool,
        reg_timestep: torch.Tensor,
        **kwargs
    ):
        with torch.no_grad():
            bs, c, h, w = latent_model_input.shape
            # if we have a control on the channel dimension, put it on the batch for packing
            has_control = False
            c = latent_model_input.shape[1]
            block = 16                         

            if c % block == 0 and c // block >= 2:
                n = c // block                    
                chunks = torch.chunk(latent_model_input, n, dim=1)
                lat = chunks[0]

                if n == 2:
                    # 只有 control，没有 origin_control
                    control, = chunks[1:]
                    latent_model_input = torch.cat([lat, control], dim=0)

                elif n == 3:
                    # 同时有 control 和 origin_control
                    control, origin_control = chunks[1:]

                    # (B,) → (B,1,1,1) 便于广播到 (B,C,H,W)
                    mask = (timestep < reg_timestep).view(-1, 1, 1, 1)      # True → 选 origin_control

                    # 逐样本选择：True 用 origin_control，否则用 control
                    selected = torch.where(mask, origin_control, control)   # 形状仍是 (B,C,H,W)

                    # 最终按 batch 维拼接：lat 在上，selected 在下 → 2B 条样本
                    latent_model_input = torch.cat([lat, selected], dim=0)

                else:
                    # n > 3 的兜底处理，逻辑保持不变
                    latent_model_input = torch.cat(chunks, dim=0)

                has_control = True

            latent_model_input_packed = rearrange(
                latent_model_input,
                "b c (h ph) (w pw) -> b (h w) (c ph pw)",
                ph=2,
                pw=2
            )

            img_ids = torch.zeros(h // 2, w // 2, 3)
            img_ids[..., 1] = img_ids[..., 1] + torch.arange(h // 2)[:, None]
            img_ids[..., 2] = img_ids[..., 2] + torch.arange(w // 2)[None, :]
            img_ids = repeat(img_ids, "h w c -> b (h w) c",
                             b=bs).to(self.device_torch)
            
            # handle control image ids
            if has_control:
                ctrl_ids = img_ids.clone()
                ctrl_ids[..., 0] = 1
                img_ids = torch.cat([img_ids, ctrl_ids], dim=1)
                
            txt_ids = torch.zeros(
                bs, text_embeddings.text_embeds.shape[1], 3).to(self.device_torch)

            # # handle guidance
            if self.unet_unwrapped.config.guidance_embeds:
                if isinstance(guidance_embedding_scale, list):
                    guidance = torch.tensor(
                        guidance_embedding_scale, device=self.device_torch)
                else:
                    guidance = torch.tensor(
                        [guidance_embedding_scale], device=self.device_torch)
                    # guidance = guidance.expand(latent_model_input.shape[0])
                    guidance = guidance.expand(timestep.shape[0])
            else:
                guidance = None

        if bypass_guidance_embedding:
            bypass_flux_guidance(self.unet)

        cast_dtype = self.unet.dtype
        # changes from orig implementation
        if txt_ids.ndim == 3:
            txt_ids = txt_ids[0]
        if img_ids.ndim == 3:
            img_ids = img_ids[0]
        
        latent_size = latent_model_input_packed.shape[1]
        # move the kontext channels. We have them on batch dimension to here, but need to put them on the latent dimension
        if has_control:
            latent, control = torch.chunk(latent_model_input_packed, 2, dim=0)
            latent_model_input_packed = torch.cat(
                [latent, control], dim=1
            )
            latent_size = latent.shape[1]

        noise_pred = self.unet(
            hidden_states=latent_model_input_packed.to(
                self.device_torch, cast_dtype),
            timestep=timestep / 1000,
            encoder_hidden_states=text_embeddings.text_embeds.to(
                self.device_torch, cast_dtype),
            pooled_projections=text_embeddings.pooled_embeds.to(
                self.device_torch, cast_dtype),
            txt_ids=txt_ids,
            img_ids=img_ids,
            guidance=guidance,
            return_dict=False,
            **kwargs,
        )[0]
        
        # remove kontext image conditioning
        noise_pred = noise_pred[:, :latent_size]

        if isinstance(noise_pred, QTensor):
            noise_pred = noise_pred.dequantize()

        noise_pred = rearrange(
            noise_pred,
            "b (h w) (c ph pw) -> b c (h ph) (w pw)",
            h=latent_model_input.shape[2] // 2,
            w=latent_model_input.shape[3] // 2,
            ph=2,
            pw=2,
            c=self.vae.config.latent_channels
        )

        if bypass_guidance_embedding:
            restore_flux_guidance(self.unet)
        
        return noise_pred
    
    def get_prompt_embeds(self, prompt: str) -> PromptEmbeds:
        if self.pipeline.text_encoder.device != self.device_torch:
            self.pipeline.text_encoder.to(self.device_torch)
        prompt_embeds, pooled_prompt_embeds = train_tools.encode_prompts_flux(
            self.tokenizer,
            self.text_encoder,
            prompt,
            max_length=512,
        )
        pe = PromptEmbeds(
            prompt_embeds
        )
        pe.pooled_embeds = pooled_prompt_embeds
        return pe
    
    def get_model_has_grad(self):
        # return from a weight if it has grad
        return self.model.proj_out.weight.requires_grad

    def get_te_has_grad(self):
        # return from a weight if it has grad
        return self.text_encoder[1].encoder.block[0].layer[0].SelfAttention.q.weight.requires_grad
    
    def save_model(self, output_path, meta, save_dtype):
        # only save the unet
        transformer: FluxTransformer2DModel = unwrap_model(self.model)
        transformer.save_pretrained(
            save_directory=os.path.join(output_path, 'transformer'),
            safe_serialization=True,
        )

        meta_path = os.path.join(output_path, 'aitk_meta.yaml')
        with open(meta_path, 'w') as f:
            yaml.dump(meta, f)

    def get_loss_target(self, *args, **kwargs):
        noise = kwargs.get('noise')
        batch = kwargs.get('batch')
        return (noise - batch.latents).detach()

    def condition_noisy_latents(self, latents: torch.Tensor, batch: "DataLoaderBatchDTO"):
        """
        将 control / origin_control 编码成 VAE latent 后与主 latent 在通道维拼接。
        - control_tensor、origin_control_tensor 先转到 VAE 所在设备并归一化到 [-1,1]。
        - 若尺寸与目标 H×W 不一致，按 bilinear 插值到相同分辨率。
        """
        with torch.no_grad():
            control_tensor = batch.control_tensor
            origin_control_tensor = batch.origin_control_tensor

            # 如果两者都为空，直接返回原 latents
            if control_tensor is None and origin_control_tensor is None:
                return latents.detach() 

            self.vae.to(self.device_torch)            # 确保 VAE 在正确设备

            # ---------- 1. 计算目标分辨率 ----------
            if batch.tensor is not None:
                target_h, target_w = batch.tensor.shape[2:]
            else:  # caching latents 时，batch.tensor 为空
                target_h = batch.file_items[0].crop_height
                target_w = batch.file_items[0].crop_width

            # ---------- 2. 统一处理函数 ----------
            def _preprocess_control(t: torch.Tensor) -> torch.Tensor:
                """归一化 [-1,1]、转 dtype/device、插值到目标分辨率"""
                if t is None:
                    return None
                t = (t * 2 - 1).to(self.vae_device_torch, dtype=self.torch_dtype)  # 归一化 + 迁移
                if t.shape[-2:] != (target_h, target_w):                           # 尺寸不符就插值
                    t = F.interpolate(t, size=(target_h, target_w), mode="bilinear")
                return t

            control_tensor        = _preprocess_control(control_tensor)
            origin_control_tensor = _preprocess_control(origin_control_tensor)

            # ---------- 3. 编码成 latent ----------
            cat_latents = [latents]          # 先放主 latent
            for t in (control_tensor, origin_control_tensor):
                if t is not None:
                    encoded = self.encode_images(t).to(latents.device, latents.dtype)
                    cat_latents.append(encoded)

            # 按通道维拼接 (B, C_total, H', W')
            latents = torch.cat(cat_latents, dim=1)

        return latents.detach() 

    def get_base_model_version(self):
        return "flux.1_kontext"