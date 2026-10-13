"""Full-graph candidate memory dynamics with explicit stimulation and checkpoints."""

import ctypes as C
import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from coinfly.research.assets import NeuralPaths, VerifiedNativeKernel
from coinfly.research.checkpoint import validate_checkpoint_archive, validate_native_state

from .circuit import identify
from .common import digest
from .rule import PARAMETERS as RULE_PARAMETERS
from .rule import RuleParameters
from .state import NativeBrain

MODEL = "coinfly-dual-compartment-v1"


class PlasticityStep(Protocol):
    def begin_frame(self, now: datetime, credits: Any, *, has_reward: bool) -> None: ...
    def finalize_frame(self, now: datetime, side: str) -> bool: ...
    def set_reinforcement(self, signal: float, stressed_return: float, booked_fee_return: float,
                          *, net_return: float) -> None: ...
    def advance(self, brain: Any, counts: Any, seconds: float, *, learning: bool,
                stimulation: Any, delta_limit: float) -> None: ...


class BrainParameters(RuleParameters):
    neural_dt_ms: float
    modulator_delivery_trace_ms: float
    kc_rest_mV: float
    kc_adaptation_jump_mV: float
    kc_adaptation_tau_ms: float


PARAMETERS: BrainParameters = {
    **RULE_PARAMETERS,
    "neural_dt_ms": 0.1,
    "modulator_delivery_trace_ms": 100.0,
    "kc_rest_mV": -60.0,
    "kc_adaptation_jump_mV": 8.0,
    "kc_adaptation_tau_ms": 200.0,
    "interpretation": "Candidate KC adaptation/rest plus a baseline-centered anti-Hebbian rate-rule extension to two compartments. No fitted DAN/MBON background current; lamina bias is a display proxy. Gain, trace constants and transfer to this graph remain unvalidated assumptions.",
}



class MemoryBrain(NativeBrain):
    def __init__(
        self,
        paths: NeuralPaths,
        kernel: VerifiedNativeKernel,
        *,
        eta: float = 0.001,
        circuit: dict[str, Any] | None = None,
        modulation_mask: Any = None,
        tonic: Any = None,
        dan_baseline_hz: Any = None,
        kc_rest: float = -60.0,
        adaptation_jump: float = 8.0,
        adaptation_tau: float = 200.0,
    ) -> None:
        if (circuit is not None or modulation_mask is not None or tonic is not None
                or dan_baseline_hz is not None or eta != 0.001 or kc_rest != -60.0
                or adaptation_jump != 8.0 or adaptation_tau != 200.0):
            raise ValueError("scientific parameter changes require a separately qualified model profile")
        self.paths = paths
        super().__init__(paths.graph)
        self.build = {"model": MODEL, "source_sha256": kernel.attestation.source_sha256,
                      "binary_sha256": kernel.attestation.binary_sha256,
                      "signed_identity": kernel.attestation.signed_identity}
        self.library = kernel.load()
        self.advance: Any = self.library.memory_advance
        argument_types: list[Any] = (
            [C.c_int]
            + [C.c_void_p] * 11
            + [C.c_int, C.c_float]
            + [C.c_void_p] * 5
            + [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_int]
            + [C.c_void_p] * 4
            + [
                C.c_float,
                C.c_float,
                C.c_float,
                C.c_int,
                C.c_void_p,
                C.c_void_p,
                C.c_void_p,
                C.c_void_p,
                C.c_void_p,
                C.c_float,
                C.c_float,
            ]
        )
        self.advance.argtypes = argument_types
        self.advance.restype = None
        self.circuit = identify(self) if circuit is None else circuit
        if not math.isfinite(kc_rest) or not -80 <= kc_rest <= -45:
            raise ValueError("Invalid KC resting potential")
        self.rest = np.full(self.n, -52.0, dtype=np.float32)
        self.rest[self.circuit["kc"]] = kc_rest
        self.v[:] = self.rest
        if (
            not math.isfinite(adaptation_jump)
            or adaptation_jump < 0
            or not math.isfinite(adaptation_tau)
            or adaptation_tau <= 20
        ):
            raise ValueError("Invalid adaptation parameters")
        self.adaptation = np.zeros(self.n, dtype=np.float32)
        self.adaptation_jump = float(adaptation_jump)
        self.adaptation_tau = float(adaptation_tau)
        if modulation_mask is None:
            import pyarrow.feather as feather

            neurons = (
                feather.read_table(paths.neurons)
                .to_pandas()
                .set_index("source_id")
                .loc[self.ids]
            )
            modulation_mask = neurons.neurotransmitter.isin(
                ["dopamine", "octopamine", "serotonin"]
            ).to_numpy(dtype=np.uint8)
        self.modulation_mask = np.asarray(modulation_mask, dtype=np.uint8).copy()
        if self.modulation_mask.shape != (self.n,) or np.any(self.modulation_mask > 1):
            raise ValueError("Invalid modulation mask")
        self.eta = float(eta)
        if not math.isfinite(self.eta) or self.eta < 0:
            raise ValueError("Finite nonnegative eta required")
        self.eligibility = np.zeros(self.n, dtype=np.float64)
        self.eligibility_last = np.zeros(self.n, dtype=np.int64)
        self.modulation = np.zeros(self.n, dtype=np.float32)
        self.modulation_last = np.zeros(self.n, dtype=np.int64)
        self.baseline_plastic = self.weight[self.circuit["edges"]].copy()
        self.initial_weight_sha256 = digest(self.weight)
        self.fields = [
            "v",
            "g",
            "refractory",
            "drive",
            "previous_drive",
            "queue",
            "queue_count",
            "counts",
            "luminance",
            "active",
            "active_flag",
            "nactive",
            "last",
            "eligibility",
            "eligibility_last",
            "modulation",
            "modulation_last",
            "adaptation",
        ]
        self.initial = {k: getattr(self, k).copy() for k in self.fields}
        from .rule import PARAMETERS as RULE_PARAMETERS

        self.rule_parameters = RULE_PARAMETERS.copy()
        self.rate_kc = np.zeros(len(self.circuit["edges"]), dtype=np.float64)
        self.rate_dan = np.zeros(len(self.circuit["dan"]), dtype=np.float64)
        self.memory_u = np.zeros_like(self.rate_kc)
        self.memory_w = np.zeros_like(self.rate_kc)
        self.tonic = (
            np.zeros(self.n, dtype=np.float32)
            if tonic is None
            else np.asarray(tonic, dtype=np.float32).copy()
        )
        self.dan_baseline_hz = (
            np.zeros(len(self.circuit["dan"]), dtype=np.float64)
            if dan_baseline_hz is None
            else np.asarray(dan_baseline_hz, dtype=np.float64).copy()
        )
        if self.tonic.shape != (self.n,) or not np.isfinite(self.tonic).all():
            raise ValueError("Invalid tonic current")
        if (
            self.dan_baseline_hz.shape != (len(self.rate_dan),)
            or not np.isfinite(self.dan_baseline_hz).all()
        ):
            raise ValueError("Invalid DAN baseline")
        self.weights_frozen = False
        self.plasticity_diagnostics: dict[str, float | int] = {}
        for k in ["rate_kc", "rate_dan", "memory_u", "memory_w"]:
            self.fields.append(k)
            self.initial[k] = getattr(self, k).copy()

    def reset(self, keep_memory: bool = False) -> None:
        self.plasticity_diagnostics = {}
        if keep_memory:
            saved = (self.memory_u.copy(), self.memory_w.copy())
        for k, v in self.initial.items():
            getattr(self, k)[:] = v
        self.cursor = 0
        self.sim_ms = 0.0
        self.total_spikes = 0
        if not keep_memory:
            self.weight[self.circuit["edges"]] = self.baseline_plastic
        else:
            self.memory_u[:], self.memory_w[:] = saved

    def _neural_step(
        self,
        luminance: Any,
        duration_ms: float,
        *,
        learning: bool = False,
        stimulation: Any = None,
        lamina_bias: float = 12.0,
    ) -> tuple[Any, float]:
        light = np.asarray(luminance)
        if light.shape != (len(self.retina),) or not np.isfinite(light).all():
            raise ValueError("Invalid retinal input")
        steps = round(duration_ms / self.dt)
        if (
            not math.isfinite(duration_ms)
            or steps < 1
            or not math.isfinite(lamina_bias)
        ):
            raise ValueError("Invalid interval/current")
        self.luminance += (1 - math.exp(-steps * self.dt / 10)) * (
            np.clip(light, 0, 1) - self.luminance
        )
        self.drive.fill(0)
        self.drive[self.lamina] = lamina_bias
        self.drive[self.retina] = 30 * self.luminance / (0.02 + self.luminance)
        self.drive += self.tonic
        if stimulation is not None:
            pulses = stimulation if isinstance(stimulation, list) else [stimulation]
            for indices, current in pulses:
                ix = np.asarray(indices, dtype=np.int32)
                amplitude = np.asarray(current, dtype=np.float32)
                if (
                    ix.ndim != 1
                    or np.any(ix < 0)
                    or np.any(ix >= self.n)
                    or not np.isfinite(amplitude).all()
                    or amplitude.shape not in [(), ix.shape]
                ):
                    raise ValueError("Invalid external stimulation")
                self.drive[ix] += amplitude
        self.counts.fill(0)
        clock = np.asarray([self.cursor], dtype=np.int64)
        c = self.circuit
        arrays: list[NDArray[Any]] = [
            self.ptr,
            self.post,
            self.weight,
            self.v,
            self.g,
            self.refractory,
            self.drive,
            self.previous_drive,
            self.queue,
            self.queue_count,
            clock,
        ]
        start = time.perf_counter()
        self.advance(
            self.n,
            *[x.ctypes.data for x in arrays],
            steps,
            self.dt,
            *[
                getattr(self, k).ctypes.data
                for k in ["counts", "active", "active_flag", "nactive", "last"]
            ],
            c["kc_mask"].ctypes.data,
            c["dan_index"].ctypes.data,
            self.eligibility.ctypes.data,
            self.eligibility_last.ctypes.data,
            len(c["edges"]),
            c["edges"].ctypes.data,
            c["pre"].ctypes.data,
            self.baseline_plastic.ctypes.data,
            c["gain"].ctypes.data,
            self.eta,
            PARAMETERS["trace_kc_seconds"] * 1000,
            PARAMETERS["minimum_fraction"],
            int(learning),
            self.modulation.ctypes.data,
            self.modulation_last.ctypes.data,
            self.modulation_mask.ctypes.data,
            self.rest.ctypes.data,
            self.adaptation.ctypes.data,
            self.adaptation_jump,
            self.adaptation_tau,
        )
        elapsed = time.perf_counter() - start
        self.cursor = int(clock[0])
        self.sim_ms = self.cursor * self.dt
        self.total_spikes += int(self.counts.sum())
        return self.counts.copy(), elapsed

    def prepare_experimental_frame(self, engine: PlasticityStep, now: datetime,
                                   credits: Any, *, has_reward: bool, signal: float = 0.,
                                   net_return: float = 0., stressed_return: float = 0.,
                                   booked_fee_return: float = 0.) -> None:
        """Causal market-clock maintenance before any neural bins or capacity use.

        The engine owns the ring. A full live ring denies order admission while
        this brain continues propagation and pending dopamine capture. No tag is
        stored here: the decoded side is only known at finalize_experimental_frame.
        """
        values = (signal, net_return, stressed_return, booked_fee_return)
        if (any(not math.isfinite(v) for v in values) or not -1 <= signal <= 1
                or booked_fee_return < 0 or (not has_reward and any(v != 0 for v in values))):
            raise ValueError("completed finite reward and booked fee evidence required")
        if not self.weights_frozen:
            # Validate the causal clock before changing gain or neural state.
            engine.begin_frame(now, credits, has_reward=has_reward)
            engine.set_reinforcement(signal, stressed_return, booked_fee_return, net_return=net_return)

    def finalize_experimental_frame(self, engine: PlasticityStep, now: datetime, side: str,
                                    *, learning: bool) -> dict[str, bool | str | None]:
        """Retain admitted order tags after decoding; HOLD never stores a tag."""
        if side not in ("BUY", "SELL", "HOLD"):
            raise ValueError("fixed decoder returned an invalid side")
        allowed = not learning or self.weights_frozen or engine.finalize_frame(now, side)
        return {"allowed": allowed, "reason": None if allowed else "eligibility_capacity"}

    def step(
        self,
        luminance: Any,
        duration_ms: float,
        *,
        learning: bool = False,
        stimulation: Any = None,
        lamina_bias: float = 12.0,
        plasticity_delta_limit: float | None = None,
        experimental_plasticity: PlasticityStep | None = None,
    ) -> tuple[Any, float]:
        from .rule import advance

        if plasticity_delta_limit is not None and (
                not math.isfinite(plasticity_delta_limit) or not 0 < plasticity_delta_limit <= 0.1):
            raise ValueError("Invalid experimental plasticity delta limit")
        if experimental_plasticity is not None and plasticity_delta_limit is None:
            raise ValueError("experimental plasticity requires a declared update limit")
        if (not math.isfinite(duration_ms) or not self.dt <= duration_ms <= 60_000
                or abs(duration_ms / self.dt - round(duration_ms / self.dt)) > 1e-7):
            raise ValueError("Invalid duration")
        remaining = round(duration_ms / self.dt)
        if remaining < 1:
            raise ValueError("Duration too short")
        total = np.zeros(self.n, dtype=np.int32)
        wall = 0.0
        while remaining:
            ticks = min(100, remaining)
            interval = ticks * self.dt
            # The original LTD update is disabled. Only the centered rule below
            # writes candidate memory efficacies; all neural integration remains.
            c, t = self._neural_step(
                luminance,
                interval,
                learning=False,
                stimulation=stimulation,
                lamina_bias=lamina_bias,
            )
            seconds = interval / 1000
            if experimental_plasticity is not None:
                assert plasticity_delta_limit is not None
                experimental_plasticity.advance(self, c, seconds, learning=learning,
                    stimulation=stimulation, delta_limit=plasticity_delta_limit)
                # The compiled experimental path writes each plastic weight in
                # place; rebuilding an indexed NumPy vector here allocates per bin.
                total += c
                wall += t
                remaining -= ticks
                continue
            prior = ((self.memory_u.copy(), self.memory_w.copy())
                     if plasticity_delta_limit is not None and not self.weights_frozen else None)
            advance(
                self.rate_kc,
                self.rate_dan,
                self.memory_u,
                self.memory_w,
                c[self.circuit["pre"]] / seconds,
                c[self.circuit["dan"]] / seconds - self.dan_baseline_hz,
                self.circuit["gain"],
                seconds,
                self.eta,
                learning,
                self.weights_frozen,
                diagnostics=self.plasticity_diagnostics,
            )
            if prior is not None:
                # A bounded experimental trust region, NOT a policy-gradient
                # optimizer. Apply to both drive and expressed efficacy; bounding
                # only weights would leave a hidden, saturated drive reservoir.
                assert plasticity_delta_limit is not None
                limit = plasticity_delta_limit * interval / 10.0
                for state, before in zip((self.memory_u, self.memory_w), prior):
                    delta = state - before
                    metrics = self.plasticity_diagnostics
                    metrics["trust_region_clipped"] = int(metrics.get("trust_region_clipped", 0)) + int(
                        np.count_nonzero(np.abs(delta) > limit))
                    metrics["maximum_unbounded_delta"] = max(
                        float(metrics.get("maximum_unbounded_delta", 0)), float(np.max(np.abs(delta), initial=0)))
                    np.clip(delta, -limit, limit, out=delta)
                    state[:] = before + delta
            if not self.weights_frozen:
                self.weight[self.circuit["edges"]] = self.baseline_plastic * (
                    1 + self.memory_w
                )
            total += c
            wall += t
            remaining -= ticks
        self.counts[:] = total
        return total, wall

    def memory(self) -> dict[str, Any]:
        w = self.weight[self.circuit["edges"]]
        fraction = w / self.baseline_plastic
        return {
            "plastic_edges": len(w),
            "changed_edges": int(np.count_nonzero(w != self.baseline_plastic)),
            "mean_efficacy": float(fraction.mean()),
            "minimum_efficacy": float(fraction.min()),
            "maximum_efficacy": float(fraction.max()),
            "floor_edges": int(np.count_nonzero(fraction <= PARAMETERS["minimum_fraction"] + 1e-6)),
            "ceiling_edges": int(np.count_nonzero(fraction >= PARAMETERS["maximum_fraction"] - 1e-6)),
            "sha256": digest(w),
            "model": MODEL,
            "plasticity_diagnostics": dict(self.plasticity_diagnostics),
        }

    def checkpoint(self, path: Path, *, controller_profile: dict[str, Any] | None = None) -> None:
        metadata = {
            "model": MODEL,
            "build": self.build,
            "eta": self.eta,
            "parameters": PARAMETERS,
            "cursor": self.cursor,
            "weights_frozen": self.weights_frozen,
            "total_spikes": self.total_spikes,
            "graph_ids_sha256": digest(self.ids),
            "graph_ptr_sha256": digest(self.ptr),
            "graph_post_sha256": digest(self.post),
            "plastic_edges_sha256": digest(self.circuit["edges"]),
            "configuration_sha256": self.configuration_signature(),
            "controller_profile": controller_profile,
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Distinct controllers/targets must never share a .partial filename.
        # Each completed archive replaces its destination atomically.
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".partial", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                np.savez_compressed(
                    handle,
                    metadata=json.dumps(metadata),
                    weight=self.weight,
                    **{k: getattr(self, k) for k in self.fields},
                )
            except BaseException:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def restore(self, path: Path, *, controller_profile: dict[str, Any] | None = None) -> None:
        validate_checkpoint_archive(path, {name: getattr(self, name) for name in ["weight", *self.fields]})
        with np.load(path, allow_pickle=False) as a:
            m = json.loads(str(a["metadata"]))
            if not isinstance(m, dict) or not {"cursor", "total_spikes", "weights_frozen"} <= m.keys():
                raise ValueError("Invalid checkpoint metadata")
            # Decode each bounded array once; NpzFile does not cache decompressed
            # members, so repeated indexing multiplies full-graph restore work.
            state = {name: a[name] for name in ["weight", *self.fields]}
            expected = {
                "model": MODEL,
                "build": self.build,
                "eta": self.eta,
                "parameters": PARAMETERS,
                "graph_ids_sha256": digest(self.ids),
                "graph_ptr_sha256": digest(self.ptr),
                "graph_post_sha256": digest(self.post),
                "plastic_edges_sha256": digest(self.circuit["edges"]),
                "controller_profile": controller_profile,
            }
            if (any(m.get(k) != v for k, v in expected.items())
                    or m.get("configuration_sha256") != self.configuration_signature()):
                raise ValueError("Checkpoint provenance mismatch")
            for k in ["weight", *self.fields]:
                if (
                    state[k].shape != getattr(self, k).shape
                    or state[k].dtype != getattr(self, k).dtype
                ):
                    raise ValueError("Checkpoint array mismatch")
                if state[k].dtype.kind == "f" and not np.isfinite(state[k]).all():
                    raise ValueError("Nonfinite checkpoint state")
            validate_native_state({k: state[k] for k in self.fields}, neuron_count=self.n, cursor=m["cursor"])
            if type(m["total_spikes"]) is not int or m["total_spikes"] < 0 or type(m["weights_frozen"]) is not bool:
                raise ValueError("Invalid checkpoint counters")
            mask = np.ones(len(self.weight), dtype=np.bool_)
            mask[self.circuit["edges"]] = False
            if not np.array_equal(state["weight"][mask], self.weight[mask]):
                raise ValueError("Checkpoint altered fixed synaptic weights")
            ratios = state["weight"][self.circuit["edges"]] / self.baseline_plastic
            if np.any(ratios < PARAMETERS["minimum_fraction"] - 1e-6) or np.any(ratios > PARAMETERS["maximum_fraction"] + 1e-6):
                raise ValueError("Checkpoint plastic efficacy outside profile")
            lo, hi = PARAMETERS["minimum_fraction"] - 1, PARAMETERS["maximum_fraction"] - 1
            if any(np.any(state[k] < lo) or np.any(state[k] > hi) for k in ("memory_u", "memory_w")):
                raise ValueError("Checkpoint memory outside profile")
            if np.any(state["rate_kc"] < 0):
                raise ValueError("Checkpoint KC rate cannot be negative")
            expected_weight = (self.baseline_plastic * (1 + state["memory_w"])).astype(self.weight.dtype)
            if not np.array_equal(state["weight"][self.circuit["edges"]], expected_weight):
                raise ValueError("Checkpoint weight and memory disagree")
            if controller_profile is not None and m["weights_frozen"] != (not controller_profile["learning"]):
                raise ValueError("Checkpoint learning mode disagrees with controller profile")
            for k in ["weight", *self.fields]:
                getattr(self, k)[:] = state[k]
            self.cursor = int(m["cursor"])
            self.sim_ms = self.cursor * self.dt
            self.total_spikes = int(m["total_spikes"])
            self.weights_frozen = bool(m["weights_frozen"])
            self.plasticity_diagnostics = {}

    def configuration_signature(self) -> dict[str, Any]:
        # Equal cell IDs and CSR endpoints alone do not imply equal input
        # geometry, original efficacies or compartment assignment.
        return {
            "initial_weight": self.initial_weight_sha256,
            "modulation_mask": digest(self.modulation_mask),
            "rule": self.rule_parameters,
            "rule_sha256": hashlib.sha256(
                Path(__file__).with_name("rule.py").read_bytes()
            ).hexdigest(),
            "tonic": digest(self.tonic),
            "dan_baseline_hz": digest(self.dan_baseline_hz),
            "rest": digest(self.rest),
            "adaptation_jump": self.adaptation_jump,
            "adaptation_tau": self.adaptation_tau,
            **{
                k: digest(getattr(self, k)) for k in ["retina", "uv", "lamina", "sugar"]
            },
            **{
                k: digest(self.circuit[k])
                for k in ["pre", "gain", "kc_mask", "dan_index"]
            },
        }
