#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Train Parallel WaveGAN."""

import argparse

import numpy as np
import torch
import yaml

from torch.utils.data import DataLoader

from parallel_wavegan.losses import MultiResolutionSTFTLoss
from parallel_wavegan.models import ParallelWaveGANDiscriminator
from parallel_wavegan.models import ParallelWaveGANGenerator
from parallel_wavegan.optimizers import RAdam
from parallel_wavegan.utils.dataset import PyTorchDataset


class CustomCollater(object):
    """Customized collater for Pytorch DataLoader."""

    def __init__(self,
                 batch_max_steps=20480,
                 hop_size=256,
                 aux_context_window=2,
                 device=torch.device("cpu")
                 ):
        """Initialize customized collater."""
        if batch_max_steps % hop_size != 0:
            batch_max_steps += -(batch_max_steps % hop_size)
        assert batch_max_steps % hop_size == 0
        self.batch_max_steps = batch_max_steps
        self.batch_max_frames = batch_max_steps // hop_size
        self.hop_size = hop_size
        self.aux_context_window = aux_context_window
        self.device = device

    def __call__(self, batch):
        """Convert into batch tensors.

        Args:
            batch (list): list of tuple of the pair of audio and features.

        Returns:
            Tensor: Gaussian noise batch (B, 1, T).
            Tensor: Auxiliary feature batch (B, C, T").
            Tensor: Target signal batch (B, 1, T).
            LongTensor: Input length batch (B,)

        """
        # Time resolution adjustment
        new_batch = []
        for idx in range(len(batch)):
            x, c = batch[idx]
            self._assert_ready_for_upsampling(x, c, self.hop_size, 0)
            if len(x) > self.batch_max_steps:
                interval_start = self.aux_context_window
                interval_end = len(c) - self.batch_max_frames - self.aux_context_window
                start_frame = np.random.randint(interval_start, interval_end)
                start_step = start_frame * self.hop_size
                x = x[start_step: start_step + self.batch_max_steps]
                c = c[start_frame - self.aux_context_window:
                      start_frame + self.aux_context_window + self.batch_max_frames]
                self._assert_ready_for_upsampling(x, c, self.hop_size, self.aux_context_window)
            new_batch.append((x, c))
        batch = new_batch

        # Make padded target signale batch
        xlens = [len(b[0]) for b in batch]
        max_olen = max(xlens)
        y_batch = np.array([self._pad_2darray(b[0].reshape(-1, 1), max_olen) for b in batch], dtype=np.float32)
        y_batch = torch.FloatTensor(y_batch).transpose(2, 1).to(self.device)

        # Make padded conditional auxiliary feature batch
        clens = [len(b[1]) for b in batch]
        max_clen = max(clens)
        c_batch = np.array([self._pad_2darray(b[1], max_clen) for b in batch], dtype=np.float32)
        c_batch = torch.FloatTensor(c_batch).transpose(2, 1).to(self.device)

        # Make input noise signale batch
        z_batch = torch.randn(y_batch.size()).to(self.device)

        # Make the list of the length of input signals
        input_lengths = torch.LongTensor(xlens).to(self.device)

        return z_batch, c_batch, y_batch, input_lengths

    @staticmethod
    def _assert_ready_for_upsampling(x, c, hop_size, context_window):
        assert len(x) == (len(c) - 2 * context_window) * hop_size

    @staticmethod
    def _pad_2darray(x, max_len, b_pad=0, constant_values=0):
        return np.pad(x, [(b_pad, max_len - len(x) - b_pad), (0, 0)],
                      mode="constant", constant_values=constant_values)


def main():
    """Run main process."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-dumpdir", default=None, type=str,
                        help="Directory including trainning data.")
    parser.add_argument("--dev-dumpdir", default=None, type=str,
                        help="Direcotry including development data.")
    parser.add_argument("--resume", default=None, type=str,
                        help="Checkpoint file path to resume training.")
    parser.add_argument("--config", default="hparam.yml", type=str,
                        help="Yaml format configuration file.")
    args = parser.parse_args()

    # load config
    with open(args.config) as f:
        config = yaml.load(f, Loader=yaml.Loader)

    # get dataset
    dataset = {}
    dataset["train"] = PyTorchDataset(args.train_dumpdir)
    dataset["dev"] = PyTorchDataset(args.dev_dumpdir)

    # get data loader
    collate_fn = CustomCollater(
        batch_max_steps=config["batch_max_steps"],
        hop_size=config["hop_size"],
        aux_context_window=config["generator"]["aux_context_window"],
        device=torch.device("cpu"),
    )
    data_loader = {}
    data_loader["train"] = DataLoader(
        dataset=dataset["train"],
        shuffle=True,
        collate_fn=collate_fn,
        batch_size=config["batch_size"],
        num_workers=config["num_workers"],
        pin_memory=config["pin_memory"],
    )
    data_loader["dev"] = DataLoader(
        dataset=dataset["dev"],
        shuffle=True,
        collate_fn=collate_fn,
        batch_size=config["batch_size"],
        num_workers=config["num_workers"],
        pin_memory=config["pin_memory"],
    )

    # define models and optimizers
    model_g = ParallelWaveGANGenerator(**config["generator"])
    model_d = ParallelWaveGANDiscriminator(**config["discriminator"])
    stft_criterion = MultiResolutionSTFTLoss(**config["stft_loss"])
    mse_criterion = torch.nn.MSELoss()
    optimizer_g = RAdam(model_g.parameters(), **config["generator_optimizer"])
    optimizer_d = RAdam(model_d.parameters(), **config["discriminator_optimizer"])
    schedular_g = torch.optim.lr_scheduler.StepLR(
        optimizer=optimizer_g,
        **config["generator_optimizer_lr_schedular"]
    )
    schedular_d = torch.optim.lr_scheduler.StepLR(
        optimizer=optimizer_d,
        **config["discriminator_optimizer_lr_schedular"]
    )
    global_steps = 0

    if args.resume is not None:
        print(f"resumed from {args.resume}.")

    while True:
        for z, c, y, input_lengths in data_loader["train"]:
            y_ = model_g(z, c)
            p_ = model_d(y_)
            y, y_, p_ = y.squeeze(1), y_.squeeze(1), p_.squeeze(1)
            adv_loss = mse_criterion(p_, p_.new_ones(p_.size()))
            aux_loss = stft_criterion(y_, y)
            loss_g = adv_loss + config["lambda_adv"] * aux_loss
            optimizer_g.zero_grad()
            loss_g.backward()
            if config["grad_norm"] > 0:
                torch.nn.utils.clip_grad_norm_(model_g.parameters(), config["grad_norm"])
            optimizer_g.step()
            schedular_g.step()

            if global_steps > config["discriminator_start_iter"]:
                y, y_ = y.unsqueeze(1), y_.unsqueeze(1).detach()
                p = model_d(y)
                p_ = model_d(y_)
                p, p_ = p.squeeze(1), p_.squeeze(1)
                loss_d = mse_criterion(p, p.new_ones(p.size())) + mse_criterion(p_, p_.new_zeros(p_.size()))
                optimizer_d.zero_grad()
                loss_d.backward()
                if config["grad_norm"] > 0:
                    torch.nn.utils.clip_grad_norm_(model_d.parameters(), config["grad_norm"])
                optimizer_d.step()
                schedular_d.step()

        global_steps += 1
        if global_steps >= config["iters"]:
            break


if __name__ == "__main__":
    main()