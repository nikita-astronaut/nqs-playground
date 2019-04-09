#!/usr/bin/env python3

# Copyright Tom Westerhout (c) 2019
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#
#     * Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
#     * Neither the name of Tom Westerhout nor the names of other
#       contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import cmath
from collections import namedtuple
from copy import deepcopy

# import cProfile
import importlib

import itertools
import logging
import math
import os
import sys
import tempfile
import time
from typing import Dict, List, Tuple, Optional

# import click
# import mpmath  # Just to be safe: for accurate computation of L2 norms
# from numba import jit, jitclass, uint8, int64, uint64, float32
# from numba.types import Bytes
# import numba.extending
import numpy as np
from numpy.polynomial import polynomial

# import scipy
# from scipy.sparse.linalg import lgmres, LinearOperator
import torch
import torch.utils.data
from torch.utils.data import TensorDataset, DataLoader

# import torch.nn as nn
# import torch.nn.functional as F

from nqs_playground.core import CompactSpin, negative_log_overlap_real, import_network, load_explicit, ExplicitState
from nqs_playground.monte_carlo import all_spins
from nqs_playground.hamiltonian import read_hamiltonian
import _C_nqs as _C


class CombiningState(torch.jit.ScriptModule):
    """
    Given a regressor ``amplitude`` predicting wave function amplitude and a
    classifier ``phase`` predicting the signs (the first class means ``+`` and
    the second -- ``-``), constructs a regressor predicting the whole wave
    function.
    """

    def __init__(self, amplitude, phase):
        super().__init__()
        self._amplitude = amplitude
        self._phase = phase

    @property
    def number_spins(self):
        return self._amplitude.number_spins

    @torch.jit.script_method
    def forward(self, x):
        A = self._amplitude(x)
        _, phi = torch.max(self._phase(x), dim=1, keepdim=True)
        phi = 1 - 2 * phi
        return A * phi.float()


def _make_checkpoints_for(n: int):
    if n <= 10:
        return list(range(0, n))
    important_iterations = list(range(0, n, n // 10))
    if important_iterations[-1] != n - 1:
        important_iterations.append(n - 1)
    return important_iterations


def optimise_scale(ψ, dataset):
    with torch.no_grad():
        x, φ = dataset
        ψ = ψ(x).view([-1])
        A = torch.dot(φ, φ).item()
        B = torch.dot(φ, ψ).item()
        assert A != 0
        return -B / A


def train_amplitude(ψ: torch.nn.Module, dataset: torch.utils.data.Dataset, config, GPU = False):
    logging.info("Learning amplitudes...")
    start = time.time()
    if GPU:
        ψ = ψ.cuda()

    epochs = config["epochs"]
    batch_size = config["batch_size"]
    optimiser = config["optimiser"](ψ)
    loss_fn = config["loss"]

    dataloader = torch.utils.data.DataLoader(
        dataset, batch_size=batch_size, shuffle=True
    )
    checkpoints = set(_make_checkpoints_for(epochs))
    for i in range(epochs):
        losses = []
        for samples, target in dataloader:
            if GPU:
                samples = samples.cuda()
                target = target.cuda()
            optimiser.zero_grad()
            loss = loss_fn(ψ(samples), target)
            losses.append(loss.item())
            loss.backward()
            optimiser.step()
        if i in checkpoints:
            losses = torch.tensor(losses)
            logging.info(
                "{}%: loss = {:.5e} ± {:.2e}; loss ∈ [{:.5e}, {:.5e}]".format(
                    100 * (i + 1) // epochs,
                    torch.mean(losses).item(),
                    torch.std(losses).item(),
                    torch.min(losses).item(),
                    torch.max(losses).item(),
                )
            )

    finish = time.time()
    logging.info("Done in {:.2f} seconds!".format(finish - start))
    if GPU:
        ψ = ψ.cpu()
    return ψ


def train_phase(ψ: torch.nn.Module, dataset: torch.utils.data.Dataset, config, GPU = False):
    logging.info("Learning phases...")
    start = time.time()

    if GPU:
        ψ = ψ.cuda()

    epochs = config["epochs"]
    batch_size = config["batch_size"]
    optimiser = config["optimiser"](ψ)
    loss_fn = config["loss"]

    dataloader = torch.utils.data.DataLoader(
        dataset, batch_size=batch_size, shuffle=True
    )
    checkpoints = set(_make_checkpoints_for(epochs))

    indices = torch.arange(len(dataset))
    samples_whole, target_whole = dataset[indices]
    if GPU:
        samples_whole = samples_whole.cuda()
        target_whole = target_whole.cuda()
    del indices

    def accuracy():
        with torch.no_grad():
            _, predicted = torch.max(ψ(samples_whole), dim=1)
            return float(torch.sum(target_whole == predicted)) / target_whole.size(0)

    logging.info("Initial accuracy: {:.2f}%".format(100 * accuracy()))
    for i in range(epochs):
        losses = []
        for samples, target in dataloader:
            if GPU:
                samples = samples.cuda()
                target = target.cuda()

            optimiser.zero_grad()
            loss = loss_fn(ψ(samples), target)
            losses.append(loss.item())
            loss.backward()
            optimiser.step()
        if i in checkpoints:
            losses = torch.tensor(losses)
            logging.info(
                "{}%: loss = {:.5e} ± {:.2e}; loss ∈ [{:.5e}, {:.5e}]".format(
                    100 * (i + 1) // epochs,
                    torch.mean(losses).item(),
                    torch.std(losses).item(),
                    torch.min(losses).item(),
                    torch.max(losses).item(),
                )
            )
    logging.info("Final accuracy: {:.2f}%".format(100 * accuracy()))

    finish = time.time()
    logging.info("Done in {:.2f} seconds!".format(finish - start))
    if GPU:
        ψ = ψ.cpu()
    return ψ


def generate_train_data(filename, config):
    explicit = False
    number_spins = config["number_spins"]
    magnetisation = config["magnetisation"]
    poly = _C.Polynomial(config["hamiltonian"], config["roots"])
    if explicit:
        if (
            "cache" not in generate_train_data.__dict__
            or generate_train_data.cache[0] != number_spins
            or generate_train_data.cache[1] != magnetisation
        ):
            generate_train_data.cache = (
                number_spins,
                magnetisation,
                all_spins(number_spins, magnetisation),
            )

        samples = generate_train_data.cache[2]
        φ = _C.TargetState(filename, poly, (8192, number_spins))(samples)
        target_amplitudes = torch.abs(φ)
        target_signs = torch.where(φ >= 0.0, torch.tensor([0]), torch.tensor([1]))
        result = None
    else:
        chain_options = _C.ChainOptions(
            number_spins=number_spins,
            magnetisation=magnetisation,
            batch_size=1024,
            steps=config["steps"],
        )
        result = _C.sample_some(filename, poly, chain_options, (16, 1))
        samples, φ, counts = result.to_tensors()
        logging.info(
            "Visited {} configurations during Monte Carlo sampling".format(
                torch.sum(counts)
            )
        )
        target_amplitudes = torch.abs(φ)
        target_signs = torch.where(φ >= 0.0, torch.tensor([0]), torch.tensor([1]))
    return samples, target_amplitudes, target_signs, result


def generate_more_data(new_state, old_state, scale, config):
    number_spins = config["number_spins"]
    magnetisation = config["magnetisation"]
    logging.info("scale = {}".format(scale))
    poly = _C.Polynomial(config["hamiltonian"], config["roots"], scale=scale)
    chain_options = _C.ChainOptions(
        number_spins=number_spins,
        magnetisation=magnetisation,
        batch_size=1024,
        steps=config["steps"],
    )
    result = _C.sample_difference(new_state, old_state, poly, chain_options, (16, 1))
    # TODO(twesterhout): This should probably happen on the C++ side...
    samples, values, _ = result.to_tensors()
    values += torch.jit.load(new_state)(samples).view([-1])
    values /= scale
    result.values(values)
    return result


def extend_using_symmetries(samples, amplitudes, symmetries):
    """
    A hacky way to extend the training data set using symmetries.
    """

    def expand(x, y):
        similar = [(x, y)]
        for p in symmetries:
            new_x = x[p]
            if np.any(new_x != x):
                similar.append((new_x, y))
        return similar

    xs = []
    ys = []
    for _, group in itertools.groupby(
        sorted(
            map(
                lambda t: (t[0].numpy().tolist(), t[1].item()),
                itertools.chain.from_iterable(
                    (expand(x, y) for x, y in zip(samples, amplitudes))
                ),
            ),
            key=lambda t: t[0],
        ),
        key=lambda t: t[0],
    ):
        group = list(group)
        x, _ = group[0]
        y = torch.mean(torch.tensor(list(map(lambda t: t[1], group))))
        xs.append(x)
        ys.append(y)
    return torch.tensor(xs), torch.tensor(ys)


def swo_step(ψ, config):
    ψ_amplitude, ψ_phase = ψ
    tempfile_name = ".temp-swo-model.old.pt"
    CombiningState(ψ_amplitude, ψ_phase).save(tempfile_name)
    logging.info("Generating the training data set...")
    samples, target_amplitudes, target_phases, result = generate_train_data(
        tempfile_name, config
    )
    logging.info("Training on {} spin configurations...".format(samples.size(0)))
    ψ_amplitude = train_amplitude(
        ψ_amplitude, TensorDataset(samples, target_amplitudes), config["amplitude"]
    )
    ψ_phase = train_phase(
        ψ_phase, TensorDataset(samples, target_phases), config["phase"]
    )

    if config["difference_sampling"]:
        logging.info("Generating more data...")
        scale = abs(optimise_scale(ψ_amplitude, (samples, target_amplitudes)))
        tempfile_name_current = ".temp-swo-model.current.pt"
        CombiningState(ψ_amplitude, ψ_phase).save(tempfile_name_current)
        extra_result = generate_more_data(
            tempfile_name_current, tempfile_name, scale, config
        )
        result.merge(extra_result)
        samples, φ, _ = result.to_tensors()
        target_amplitudes = torch.abs(φ)
        target_phases = torch.where(φ >= 0.0, torch.tensor([0]), torch.tensor([1]))
        logging.info("Training on {} spin configurations...".format(samples.size(0)))
        ψ_amplitude = train_amplitude(
            ψ_amplitude, TensorDataset(samples, target_amplitudes), config["amplitude"], GPU = True
        )
        ψ_phase = train_phase(
            ψ_phase, TensorDataset(samples, target_phases), config["phase"], GPU = True
        )
    return ψ_amplitude, ψ_phase


def switch_to_other_net(φ, ψ, config):
    φ_amplitude, φ_phase = φ
    ψ_amplitude, ψ_phase = ψ
    tempfile_name = ".swo.model.temp"
    CombiningState(ψ_amplitude, ψ_phase).save(tempfile_name)
    logging.info("Generating the training data set...")
    samples, target_amplitudes, target_phases = generate_train_data(
        tempfile_name, config
    )
    logging.info("Training on {} spin configurations...".format(samples.size(0)))
    φ_amplitude = train_amplitude(
        φ_amplitude, TensorDataset(samples, target_amplitudes), config["amplitude"]
    )
    # ψ_phase = train_phase(
    #     φ_phase, TensorDataset(samples, target_phases), config["phase"]
    # )
    return φ_amplitude, φ_phase


class FastCombiningState(torch.jit.ScriptModule):
    def __init__(self, amplitude, phase):
        super().__init__()
        self._amplitude = amplitude
        self._phase = phase

    @property
    def number_spins(self):
        return self._amplitude.number_spins

    @torch.jit.script_method
    def forward(self, x):
        A = self._amplitude(x)
        _, phi = torch.max(self._phase(x), dim=1, keepdim=True)
        phi = 1 - 2 * phi
        return A * phi.float()


def load_overlap_states(samples):
    phi_dicts = []
    phis = []
    phis_s = []
    energies = []
    labels = []

    for idx in range(20):
        f = open('./vectors/vector_' + str(idx) + '.txt', 'rb')
        energies.append(float(f.readline()[2:]))
        labels.append(int(f.readline()[2:]))

        phi_dicts.append(load_explicit(f)[0])
        phis.append(ExplicitState(phi_dicts[-1]))
        phis_s.append(phis[-1](samples))
    return phis_s, energies, labels

def print_overlaps(ψ_phase, ψ_amplitude, samples, phis_s, energies, labels):
    torch.jit.save(ψ_phase, 'phase.temp')
    torch.jit.save(ψ_amplitude, 'amplitude.temp')

    ψ_phase_overlap = torch.jit.load('phase.temp')
    ψ_amplitude_overlap = torch.jit.load('amplitude.temp')
    φ_overlap = FastCombiningState(ψ_amplitude_overlap, ψ_phase_overlap)
    psi_s = φ_overlap(samples)

    y = psi_s.detach().view(-1).numpy()
    overlap_wr_deg = 0.0
    prev_label = 0
    for idx in range(len(phis_s)): 
        x = phis_s[idx].detach().view(-1).numpy().view(dtype=np.complex64)
        if labels[idx] == prev_label:
            overlap_wr_deg += (np.abs(np.dot(x.conj(), y)) / np.linalg.norm(x) / np.linalg.norm(y)) ** 2
        else:
            logging.info("overlap with energy {} = {}".format(energies[idx - 1], np.sqrt(overlap_wr_deg)))
            prev_label = labels[idx]
            overlap_wr_deg = (np.abs(np.dot(x.conj(), y)) / np.linalg.norm(x) / np.linalg.norm(y)) ** 2


_KAGOME_18_SYMMETRIES = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
    [0, 1, 2, 3, 4, 5, 10, 9, 11, 7, 6, 8, 13, 12, 14, 16, 15, 17],
    [0, 4, 14, 3, 1, 17, 12, 16, 8, 15, 13, 11, 6, 10, 2, 9, 7, 5],
    [0, 4, 14, 3, 1, 17, 13, 15, 11, 16, 12, 8, 10, 6, 2, 7, 9, 5],
    [1, 0, 2, 4, 3, 5, 6, 7, 8, 9, 10, 11, 16, 15, 17, 13, 12, 14],
    [1, 0, 2, 4, 3, 5, 10, 9, 11, 7, 6, 8, 15, 16, 17, 12, 13, 14],
    [1, 3, 17, 4, 0, 14, 15, 13, 11, 12, 16, 8, 10, 6, 2, 7, 9, 5],
    [1, 3, 17, 4, 0, 14, 16, 12, 8, 13, 15, 11, 6, 10, 2, 9, 7, 5],
    [3, 1, 17, 0, 4, 14, 15, 13, 11, 12, 16, 8, 9, 7, 5, 6, 10, 2],
    [3, 1, 17, 0, 4, 14, 16, 12, 8, 13, 15, 11, 7, 9, 5, 10, 6, 2],
    [3, 4, 5, 0, 1, 2, 7, 6, 8, 10, 9, 11, 16, 15, 17, 13, 12, 14],
    [3, 4, 5, 0, 1, 2, 9, 10, 11, 6, 7, 8, 15, 16, 17, 12, 13, 14],
    [4, 0, 14, 1, 3, 17, 12, 16, 8, 15, 13, 11, 7, 9, 5, 10, 6, 2],
    [4, 0, 14, 1, 3, 17, 13, 15, 11, 16, 12, 8, 9, 7, 5, 6, 10, 2],
    [4, 3, 5, 1, 0, 2, 7, 6, 8, 10, 9, 11, 12, 13, 14, 15, 16, 17],
    [4, 3, 5, 1, 0, 2, 9, 10, 11, 6, 7, 8, 13, 12, 14, 16, 15, 17],
    [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 0, 1, 2, 3, 4, 5],
    [6, 7, 8, 9, 10, 11, 16, 15, 17, 13, 12, 14, 1, 0, 2, 4, 3, 5],
    [6, 10, 2, 9, 7, 5, 0, 4, 14, 3, 1, 17, 12, 16, 8, 15, 13, 11],
    [6, 10, 2, 9, 7, 5, 1, 3, 17, 4, 0, 14, 16, 12, 8, 13, 15, 11],
    [7, 6, 8, 10, 9, 11, 12, 13, 14, 15, 16, 17, 4, 3, 5, 1, 0, 2],
    [7, 6, 8, 10, 9, 11, 16, 15, 17, 13, 12, 14, 3, 4, 5, 0, 1, 2],
    [7, 9, 5, 10, 6, 2, 3, 1, 17, 0, 4, 14, 16, 12, 8, 13, 15, 11],
    [7, 9, 5, 10, 6, 2, 4, 0, 14, 1, 3, 17, 12, 16, 8, 15, 13, 11],
    [9, 7, 5, 6, 10, 2, 3, 1, 17, 0, 4, 14, 15, 13, 11, 12, 16, 8],
    [9, 7, 5, 6, 10, 2, 4, 0, 14, 1, 3, 17, 13, 15, 11, 16, 12, 8],
    [9, 10, 11, 6, 7, 8, 13, 12, 14, 16, 15, 17, 4, 3, 5, 1, 0, 2],
    [9, 10, 11, 6, 7, 8, 15, 16, 17, 12, 13, 14, 3, 4, 5, 0, 1, 2],
    [10, 6, 2, 7, 9, 5, 0, 4, 14, 3, 1, 17, 13, 15, 11, 16, 12, 8],
    [10, 6, 2, 7, 9, 5, 1, 3, 17, 4, 0, 14, 15, 13, 11, 12, 16, 8],
    [10, 9, 11, 7, 6, 8, 13, 12, 14, 16, 15, 17, 0, 1, 2, 3, 4, 5],
    [10, 9, 11, 7, 6, 8, 15, 16, 17, 12, 13, 14, 1, 0, 2, 4, 3, 5],
    [12, 13, 14, 15, 16, 17, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
    [12, 13, 14, 15, 16, 17, 4, 3, 5, 1, 0, 2, 7, 6, 8, 10, 9, 11],
    [12, 16, 8, 15, 13, 11, 6, 10, 2, 9, 7, 5, 0, 4, 14, 3, 1, 17],
    [12, 16, 8, 15, 13, 11, 7, 9, 5, 10, 6, 2, 4, 0, 14, 1, 3, 17],
    [13, 12, 14, 16, 15, 17, 0, 1, 2, 3, 4, 5, 10, 9, 11, 7, 6, 8],
    [13, 12, 14, 16, 15, 17, 4, 3, 5, 1, 0, 2, 9, 10, 11, 6, 7, 8],
    [13, 15, 11, 16, 12, 8, 9, 7, 5, 6, 10, 2, 4, 0, 14, 1, 3, 17],
    [13, 15, 11, 16, 12, 8, 10, 6, 2, 7, 9, 5, 0, 4, 14, 3, 1, 17],
    [15, 13, 11, 12, 16, 8, 9, 7, 5, 6, 10, 2, 3, 1, 17, 0, 4, 14],
    [15, 13, 11, 12, 16, 8, 10, 6, 2, 7, 9, 5, 1, 3, 17, 4, 0, 14],
    [15, 16, 17, 12, 13, 14, 1, 0, 2, 4, 3, 5, 10, 9, 11, 7, 6, 8],
    [15, 16, 17, 12, 13, 14, 3, 4, 5, 0, 1, 2, 9, 10, 11, 6, 7, 8],
    [16, 12, 8, 13, 15, 11, 6, 10, 2, 9, 7, 5, 1, 3, 17, 4, 0, 14],
    [16, 12, 8, 13, 15, 11, 7, 9, 5, 10, 6, 2, 3, 1, 17, 0, 4, 14],
    [16, 15, 17, 13, 12, 14, 1, 0, 2, 4, 3, 5, 6, 7, 8, 9, 10, 11],
    [16, 15, 17, 13, 12, 14, 3, 4, 5, 0, 1, 2, 7, 6, 8, 10, 9, 11],
]


AmplitudeNet = import_network("../nqs-playground/nqs_playground/extra_files/small.py")

PhaseNet = import_network("../nqs-playground/nqs_playground/extra_files/phase.py")

_CHAIN_10 = {
    "number_spins": 10,
    "magnetisation": 0,
    "hamiltonian": read_hamiltonian("../nqs-playground/nqs_playground/extra_files/1x10.hamiltonian").to_cxx(),
    "roots": [
        (1.0200078895671043 + 0.8629637153606778j, None),
        (1.0200078895671043 - 0.8629637153606778j, None),
    ],
    "epochs": 20,
    "output": "result.1x10/swo",
    "steps": (4, 50, 250, 1),
    "difference_sampling": True,
    "amplitude": {
        "optimiser": lambda p: torch.optim.Adam(p.parameters(), lr=0.0005),
        "epochs": 500,
        "batch_size": 32,
        "loss": lambda x, y: negative_log_overlap_real(x, y),
    },
    "phase": {
        "optimiser": lambda p: torch.optim.Adam(p.parameters(), lr=0.003),
        "epochs": 100,
        "batch_size": 64,
        "loss": torch.nn.CrossEntropyLoss(),
    },
}

_KAGOME_18 = {
    "number_spins": 18,
    "magnetisation": 0,
    "hamiltonian": read_hamiltonian("../nqs-playground/nqs_playground/extra_files/Kagome-18.hamiltonian").to_cxx(),
    "roots": [
        (1.0200078895671043 + 0.8629637153606778j, None),
        (1.0200078895671043 - 0.8629637153606778j, None),
    ],
    "epochs": 100,
    "output": "result/swo",
    "steps": (4, 100, 2100, 1),
    "difference_sampling": True,
    "amplitude": {
        "optimiser": lambda p: torch.optim.Adam(p.parameters(), lr=0.003),
        "epochs": 1000,
        "batch_size": 4096,
        "loss": lambda x, y: negative_log_overlap_real(x, y),
    },
    "phase": {
        "optimiser": lambda p: torch.optim.Adam(p.parameters(), lr=0.003),
        "epochs": 200,
        "batch_size": 4096,
        "loss": torch.nn.CrossEntropyLoss(),
    },
}

_OPTIONS = _CHAIN_10


def main():
    logging.basicConfig(
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        level=logging.DEBUG,
    )

    number_spins = _OPTIONS["number_spins"]
    # φ = torch.jit.load("result.backup/swo.model.1000.pt")
    # φ_amplitude = φ._amplitude
    # φ_phase = φ._phase
    ψ_amplitude = AmplitudeNet(number_spins)
    ψ_phase = PhaseNet(number_spins)
    for i in range(_OPTIONS["epochs"]):
        logging.info("-" * 10 + str(i + 1) + "-" * 10)
        ψ_amplitude, ψ_phase = swo_step((ψ_amplitude, ψ_phase), _OPTIONS)
        #CombiningState(ψ_amplitude, ψ_phase).save(
        #    "{}.model.{}.pt".format(_OPTIONS["output"], i + 1)
        #)


if __name__ == "__main__":
    main()
