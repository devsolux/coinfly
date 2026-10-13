"""Native-compiled experimental tag/capture and homeostasis; no graph edits.

Numba compiles these numerical kernels to machine code. The attested C++ neural
propagator remains byte-identical. This is a separately versioned hypothesis,
not a reproduction of measured MaleCNS synaptic kinetics.
"""

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any

import numpy as np
from numba import njit
from numpy.typing import NDArray

from coinfly.domain.decimal_math import financial_context
from coinfly.research.reward import DecisionCredit

Array = NDArray[np.float64]
# Per-bin output ABI: saturation, lambda, clipped steps, largest candidate delta,
# absolute saturation, erasure rate, passive loss, requested lambda, active, DAN.
BIN_METRICS = 10
MAX_CREDITS = 1024


class EligibilityCapacityError(ValueError):
    """A declared trial memory budget cannot retain its still-live decision tags."""


@dataclass(frozen=True)
class PlasticityConfig:
    neural_trace_seconds: float = 1.0
    credit_tau_seconds: float = 30.0  # market clock, distinct from simulated neural time
    maximum_credit_age_seconds: float = 150.0
    homeostasis_per_second: float = 0.001
    saturation_decay_per_second: float = 0.5
    near_bound_fraction: float = 0.95
    rate_scale_hz: float = 100.0
    dan_threshold_hz: float = 0.0
    eta_scale: float = 1.0
    positive_gain: float = 1.0
    fee_shock_gain: float = 1.0
    positive_return_threshold: float = 0.001
    maximum_homeostatic_loss_per_second: float = 0.02
    maximum_tags: int = 256
    maximum_tag_bytes: int = 64 * 1024**2

    def __post_init__(self) -> None:
        floats = [v for k, v in asdict(self).items() if k not in ("maximum_tags", "maximum_tag_bytes")]
        if any(isinstance(v, bool) or not math.isfinite(v) for v in floats):
            raise ValueError("finite plasticity parameters required")
        if (not .01 <= self.neural_trace_seconds <= 10 or not .1 <= self.credit_tau_seconds <= 150
                or not self.credit_tau_seconds <= self.maximum_credit_age_seconds <= 150
                or not 0 <= self.homeostasis_per_second <= 10
                or not 0 <= self.saturation_decay_per_second <= 10
                or not .5 <= self.near_bound_fraction < 1 or not 1 <= self.rate_scale_hz <= 10000
                or not 0 <= self.dan_threshold_hz <= 100
                or not .01 <= self.eta_scale <= 10 or not 1 <= self.positive_gain <= 3
                or not .1 <= self.fee_shock_gain <= 1 or not 1e-6 <= self.positive_return_threshold <= .1
                or not .0001 <= self.maximum_homeostatic_loss_per_second <= .02
                or type(self.maximum_tags) is not int or not 1 <= self.maximum_tags <= 4096
                or type(self.maximum_tag_bytes) is not int or not 8 <= self.maximum_tag_bytes <= 1024**3):
            raise ValueError("plasticity parameters outside bounded experimental domain")


@njit(nogil=True, fastmath=False, boundscheck=True)
def reinforcement_gain_kernel(signal: float, net_return: float, stressed_return: float,
                              booked_fee_return: float, positive_return_threshold: float,
                              positive_gain: float, fee_shock_gain: float) -> float:
    """Normalized, validated scalars only; exact fee accounting stays in Python."""
    if signal > 0 and net_return > 0 and stressed_return >= positive_return_threshold:
        return positive_gain
    # Open interval excludes break-even, exactly fee-sized and larger losses.
    if (signal < 0 and -booked_fee_return < net_return < 0
            and -booked_fee_return < stressed_return < 0):
        return fee_shock_gain
    return 1.0


@njit(nogil=True, fastmath=False, boundscheck=True)
def _tag_step(pre: float, post: float, tag: float, p: float, q: float,
              decay: float, fraction: float) -> tuple[float, float, float]:
    # Filter first. Using the old traces instead changes the discretized rule
    # by a factor of decay in the timing term.
    pre = decay * pre + fraction * p
    post = decay * post + fraction * q
    tag = max(-1.0, min(1.0, decay * tag + fraction * (p * post - q * pre)))
    return pre, post, tag


@njit(nogil=True, fastmath=False, boundscheck=True)
def tag_kernel(pre_trace: Any, post_trace: Any, tag: Any, pre_rate: Any, post_rate: Any,
               seconds: float, tau: float, scale: float) -> None:
    """Validated standalone tag update; all validation precedes state writes."""
    if (not math.isfinite(seconds) or seconds <= 0 or not math.isfinite(tau) or tau <= 0
            or not math.isfinite(scale) or scale <= 0
            or len(pre_trace) != len(tag) or len(post_trace) != len(tag)
            or len(pre_rate) != len(tag) or len(post_rate) != len(tag)):
        raise ValueError("invalid eligibility inputs")
    for i in range(len(tag)):
        if (not 0 <= pre_trace[i] <= 1 or not 0 <= post_trace[i] <= 1 or not -1 <= tag[i] <= 1
                or not math.isfinite(pre_rate[i]) or pre_rate[i] < 0
                or not math.isfinite(post_rate[i]) or post_rate[i] < 0):
            raise ValueError("invalid eligibility state or rates")
    decay = math.exp(-seconds / tau)
    fraction = -math.expm1(-seconds / tau)
    for i in range(len(tag)):
        # Compare before dividing so even finite float64 extremes normalize
        # without an overflowing intermediate.
        pre = 1.0 if pre_rate[i] >= scale else pre_rate[i] / scale
        post = 1.0 if post_rate[i] >= scale else post_rate[i] / scale
        pre_trace[i], post_trace[i], tag[i] = _tag_step(
            pre_trace[i], post_trace[i], tag[i], pre, post, decay, fraction)


@njit(nogil=True, fastmath=False, boundscheck=True)
def capture_kernel(u: Any, w: Any, credit: Any, dopamine: Any, seconds: float,
                   eta: float, base_decay: float, saturation_decay: float,
                   near: float, delta_limit: float, loss_limit: float = .02) -> Any:
    if (not 0 < seconds <= .0100001 or not 0 <= eta <= 1
            or not 0 <= base_decay <= 10 or not 0 <= saturation_decay <= 10
            or not .5 <= near < 1 or not 0 < delta_limit <= .1 or not 0 < loss_limit <= .02
            or len(w) != len(u) or len(credit) != len(u) or len(dopamine) != len(u)):
        raise ValueError("invalid capture parameters")
    lo, hi = -0.9, 1.0
    count = absolute_count = 0
    memory_present = False
    for i in range(len(u)):
        if (not lo <= u[i] <= hi or not lo <= w[i] <= hi or not -1 <= credit[i] <= 1
                or not 0 <= dopamine[i] <= 10000.01):
            raise ValueError("invalid capture state or rates")
        if u[i] <= lo * near or u[i] >= hi * near or w[i] <= lo * near or w[i] >= hi * near:
            count += 1
        if u[i] <= lo + 1e-12 or u[i] >= hi - 1e-12 or w[i] <= lo + 1e-12 or w[i] >= hi - 1e-12:
            absolute_count += 1
        memory_present = memory_present or abs(u[i]) > 1e-12 or abs(w[i]) > 1e-12
    saturation = count / max(len(u), 1)
    # Bound passive erasure per simulated second, independent of bin size.
    requested_rate = base_decay + saturation_decay * saturation
    rate = min(requested_rate, -math.log1p(-loss_limit))
    decay, smooth = math.exp(-rate * seconds), -math.expm1(-seconds / .05)
    clipped = after_count = after_absolute_count = 0
    largest = 0.0
    for i in range(len(u)):
        drive = eta * credit[i] * dopamine[i]
        # Anti-windup: outward capture vanishes at its boundary, while inward
        # capture remains effective. Baseline-anchored decay creates headroom.
        headroom = max(0.0, min(1.0, (hi - u[i]) / hi if drive > 0 else (u[i] - lo) / -lo))
        candidate_u = u[i] * decay + drive * headroom * seconds
        next_u = max(lo, min(hi, u[i] + max(-delta_limit, min(delta_limit, candidate_u - u[i]))))
        relaxed_w = w[i] * decay
        candidate_w = relaxed_w + smooth * (next_u - relaxed_w)
        du, dw = candidate_u - u[i], candidate_w - w[i]
        largest = max(largest, abs(du), abs(dw))
        clipped += int(abs(du) > delta_limit) + int(abs(dw) > delta_limit)
        u[i] = next_u
        w[i] = max(lo, min(hi, w[i] + max(-delta_limit, min(delta_limit, dw))))
        if u[i] <= lo * near or u[i] >= hi * near or w[i] <= lo * near or w[i] >= hi * near:
            after_count += 1
        if u[i] <= lo + 1e-12 or u[i] >= hi - 1e-12 or w[i] <= lo + 1e-12 or w[i] >= hi - 1e-12:
            after_absolute_count += 1
    return (max(saturation, after_count / max(len(u), 1)), rate, clipped, largest,
            max(absolute_count, after_absolute_count) / max(len(u), 1),
            rate if memory_present else 0., -math.expm1(-rate * seconds), requested_rate)


@njit(nogil=True, fastmath=False, boundscheck=True)
def plasticity_kernel(pre_trace: Any, post_trace: Any, tag: Any, credit: Any,
                      u: Any, w: Any, counts: Any, edges: Any, pre: Any, post: Any,
                      dan_ids: Any, gain: Any, baseline_hz: Any, stimulated: Any,
                      dan: Any, dopamine: Any, weights: Any, original_weights: Any,
                      metrics: Any, seconds: float, tau: float, scale: float,
                      threshold: float, eta: float, base_decay: float, saturation_decay: float,
                      near: float, delta_limit: float, loss_limit: float,
                      weight_min: float, weight_max: float) -> None:
    """One bin, no array allocation: readout, tagging, capture, efficacy, telemetry.

    The Python boundary checks array layout/ownership. Numerical/index validation
    finishes before changing any neural state or weight; only scratch DAN buffers
    may be overwritten on failure. This call runs under the controller's lock.
    """
    if (not 0 < seconds <= .0100001 or not .01 <= tau <= 10 or not 1 <= scale <= 10000
            or not 0 <= threshold <= 100 or not 0 <= eta <= 1
            or not 0 <= base_decay <= 10 or not 0 <= saturation_decay <= 10
            or not .5 <= near < 1 or not 0 < delta_limit <= .1 or not 0 < loss_limit <= .02
            or not math.isfinite(weight_max) or not 0 < weight_min < weight_max):
        raise ValueError("invalid experimental plasticity parameters")
    for j in range(len(stimulated)):
        if stimulated[j] < 0 or stimulated[j] >= len(counts):
            raise ValueError("invalid stimulated neuron")
    for j in range(len(dan_ids)):
        neuron = dan_ids[j]
        if neuron < 0 or neuron >= len(counts) or not 0 <= baseline_hz[j] <= 10000.01:
            raise ValueError("invalid DAN identity or baseline")
        observed = counts[neuron] / seconds
        if not 0 <= observed <= 10000.01:
            raise ValueError("invalid observed DAN rate")
        active = False
        for k in range(len(stimulated)):
            if stimulated[k] == neuron:
                active = True
                break
        dan[j] = max(observed - baseline_hz[j] - threshold, 0.0) if active else 0.0
    active_capture = any_dopamine = False
    for i in range(len(u)):
        edge = edges[i]
        if edge < 0 or edge >= len(post) or edge >= len(weights) or pre[i] < 0 or pre[i] >= len(counts):
            raise ValueError("invalid plastic edge identity")
        if post[edge] < 0 or post[edge] >= len(counts):
            raise ValueError("invalid MBON identity")
        p, q = counts[pre[i]] / seconds, counts[post[edge]] / seconds
        if (not 0 <= p <= 10000.01 or not 0 <= q <= 10000.01
                or not -.9 <= u[i] <= 1 or not -.9 <= w[i] <= 1
                or not 0 <= pre_trace[i] <= 1 or not 0 <= post_trace[i] <= 1
                or not -1 <= tag[i] <= 1 or not -1 <= credit[i] <= 1
                or not math.isfinite(weights[edge])
                or not -weight_max / 2 <= original_weights[i] <= weight_max / 2
                or 0 < abs(original_weights[i]) < weight_min):
            raise ValueError("invalid experimental plasticity state or rates")
        projected = total_gain = 0.0
        for j in range(len(dan_ids)):
            value = gain[j, i]
            if not 0 <= value <= 1:
                raise ValueError("invalid compartment gain")
            total_gain += value
            projected += value * dan[j]
        if abs(total_gain - 1.0) > 1e-6 or not 0 <= projected <= 10000.01:
            raise ValueError("invalid normalized compartment drive")
        dopamine[i] = projected
        any_dopamine = any_dopamine or projected > 0
        active_capture = active_capture or (credit[i] != 0 and projected > 0)
    decay, fraction = math.exp(-seconds / tau), -math.expm1(-seconds / tau)
    for i in range(len(u)):
        p = min(counts[pre[i]] / seconds / scale, 1.0)
        q = min(counts[post[edges[i]]] / seconds / scale, 1.0)
        pre_trace[i], post_trace[i], tag[i] = _tag_step(
            pre_trace[i], post_trace[i], tag[i], p, q, decay, fraction)
    result = capture_kernel(u, w, credit, dopamine, seconds, eta, base_decay,
                            saturation_decay, near, delta_limit, loss_limit)
    (metrics[0], metrics[1], metrics[2], metrics[3], metrics[4], metrics[5],
     metrics[6], metrics[7]) = result
    metrics[8] = 1.0 if active_capture else 0.0
    metrics[9] = 1.0 if any_dopamine else 0.0
    for i in range(len(u)):
        weights[edges[i]] = original_weights[i] * (1.0 + w[i])


@njit(nogil=True, fastmath=False, boundscheck=True)
def credit_kernel(values: Any, timestamps: Any, slots: Any, fractions: Any,
                  count: int, now_us: int, tau: float, out: Any) -> tuple[float, float]:
    """Capture validated allocations into caller-owned storage, without renormalizing.

    Subtract integer microseconds before converting the delay to float64. Summing
    one edge at a time avoids a temporary edge vector for every allocation.
    """
    mass = peak = 0.0
    # Reuse fraction scratch for the decayed coefficients (never the ledger).
    for j in range(count):
        delay = (now_us - timestamps[slots[j]]) / 1000000.0
        fractions[j] *= math.exp(-delay / tau)
        mass += fractions[j]
    for i in range(len(out)):
        value = correction = 0.0
        for j in range(count):
            term = values[slots[j], i] * fractions[j] - correction
            updated = value + term
            correction = (updated - value) - term
            value = updated
        out[i] = max(-1.0, min(1.0, value))
        peak = max(peak, abs(out[i]))
    return mass, peak


@njit(nogil=True, fastmath=False, boundscheck=True)
def store_kernel(values: Any, slot: int, tag: Any) -> None:
    for i in range(len(tag)):
        if not -1 <= tag[i] <= 1:
            raise ValueError("invalid eligibility tag")
    for i in range(len(tag)):
        values[slot, i] = tag[i]


def _micros(at: datetime) -> int:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware eligibility clock required")
    elapsed = at - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds


@njit(nogil=True, fastmath=False, boundscheck=True)
def expired_prefix(timestamps: Any, head: int, size: int, now_us: int, age_us: int) -> int:
    """Only the observed market clock is legal; equality retains the live tag."""
    expired = 0
    while expired < size:
        if now_us - timestamps[(head + expired) % len(timestamps)] <= age_us:
            break
        expired += 1
    return expired


class EligibilityBank:
    """Fixed-size ring; expire only the causal prefix, never evict live credit."""

    def __init__(self, edges: int, config: PlasticityConfig) -> None:
        if type(edges) is not int or edges < 1:
            raise ValueError("nonempty plastic edge set required")
        self.config = config
        self.edges = edges
        self.capacity = min(config.maximum_tags, config.maximum_tag_bytes // (edges * 8))
        if self.capacity < 1:
            raise EligibilityCapacityError("eligibility memory budget cannot hold one graph tag")
        self._values = np.empty((self.capacity, edges), dtype=np.float64)
        self._timestamps = np.empty(self.capacity, dtype=np.int64)
        self._credit_slots = np.empty(MAX_CREDITS, dtype=np.int64)
        self._credit_fractions = np.empty(MAX_CREDITS, dtype=np.float64)
        self._slots: dict[int, int] = {}
        self._dates: list[datetime | None] = [None] * self.capacity
        self._head = self._size = 0
        self._last_decision: int | None = None
        self._clock_us: int | None = None
        self._reserved: int | None = None
        self.expired_tags = self.capacity_rejections = 0

    def __len__(self) -> int:
        return self._size

    @property
    def tags(self) -> dict[datetime, Array]:
        """Owned read-only diagnostic snapshots, outside the training hot path."""
        result = {}
        for slot in self._slots.values():
            at = self._dates[slot]
            assert at is not None
            result[at] = self._values[slot].copy()
        for value in result.values():
            value.flags.writeable = False
        return result

    def clear(self) -> None:
        self._slots.clear()
        self._dates = [None] * self.capacity
        self._head = self._size = 0
        self._reserved = None

    def purge_expired(self, now: datetime) -> int:
        now_us = _micros(now)
        if self._clock_us is not None and now_us < self._clock_us:
            raise ValueError("eligibility clock cannot move backwards")
        expired = int(expired_prefix(self._timestamps, self._head, self._size, now_us,
                                    math.floor(self.config.maximum_credit_age_seconds * 1000000)))
        for offset in range(expired):
            slot = (self._head + offset) % self.capacity
            del self._slots[int(self._timestamps[slot])]
            self._dates[slot] = None
        self._head = (self._head + expired) % self.capacity
        self._size -= expired
        self.expired_tags += expired
        self._clock_us = now_us
        return expired

    def reserve(self, now: datetime) -> bool:
        now_us = _micros(now)
        if self._last_decision is not None and now_us <= self._last_decision:
            raise ValueError("eligibility decisions must strictly increase")
        self.purge_expired(now)
        self._last_decision = now_us
        self._reserved = now_us if self._size < self.capacity else None
        self.capacity_rejections += int(self._reserved is None)
        return self._reserved is not None

    def store(self, now: datetime, tag: Array) -> None:
        now_us = _micros(now)
        if (tag.shape != (self.edges,) or tag.dtype != np.float64 or not tag.flags.c_contiguous
                or self._reserved != now_us or now_us in self._slots or self._size >= self.capacity):
            raise ValueError("invalid eligibility tag or unreserved capacity")
        slot = (self._head + self._size) % self.capacity
        store_kernel(self._values, slot, tag)
        self._timestamps[slot] = now_us
        self._dates[slot] = now.astimezone(timezone.utc)
        self._slots[now_us] = slot
        self._size += 1
        self._reserved = None

    def capture(self, now: datetime, credits: tuple[DecisionCredit, ...]) -> tuple[Array, dict[str, float | int]]:
        """Owned diagnostic result; the training path uses capture_into instead."""
        result = np.zeros(self.edges, dtype=np.float64)
        return result, self.capture_into(now, credits, result)

    def capture_into(self, now: datetime, credits: tuple[DecisionCredit, ...],
                     out: Array) -> dict[str, float | int]:
        now_us = _micros(now)
        if self._clock_us is not None and now_us < self._clock_us:
            raise ValueError("eligibility clock cannot move backwards")
        if (out.shape != (self.edges,) or out.dtype != np.float64 or not out.flags.c_contiguous
                or not out.flags.writeable or np.shares_memory(out, self._values)):
            raise ValueError("independent writable float64 credit output required")
        if len(credits) > MAX_CREDITS:
            raise ValueError("too many credit allocations")
        # Revalidate at this public boundary too: capture need not arrive through
        # RewardWindow. Never round or normalize an over-allocated Decimal sum.
        seen: set[int] = set()
        with localcontext(financial_context(100)):
            total = Decimal(0)
            for credit in credits:
                at_us = _micros(credit.decision_at)
                if at_us >= now_us or at_us in seen:
                    raise ValueError("reward cannot capture a future/current or duplicate decision tag")
                if (not isinstance(credit.fraction, Decimal) or not credit.fraction.is_finite()
                        or not 0 < credit.fraction <= 1):
                    raise ValueError("invalid exact credit fraction")
                seen.add(at_us)
                total += credit.fraction
            if total > 1:
                raise ValueError("reward credit fractions exceed one")
        retained_fraction = 0.0
        missing = expired = retained = 0
        maximum_delay = 0.
        for credit in credits:
            age_us = now_us - _micros(credit.decision_at)
            age = age_us / 1000000
            maximum_delay = max(maximum_delay, age)
            too_old = age_us > math.floor(self.config.maximum_credit_age_seconds * 1000000)
            slot = self._slots.get(_micros(credit.decision_at))
            if slot is None or too_old:
                missing += 1
                expired += int(too_old)
                continue
            self._credit_slots[retained] = slot
            self._credit_fractions[retained] = float(credit.fraction)
            retained += 1
            retained_fraction += float(credit.fraction)
        mass, peak = credit_kernel(self._values, self._timestamps, self._credit_slots,
                                   self._credit_fractions, retained, now_us,
                                   self.config.credit_tau_seconds, out)
        return {"credit_decay_mass": mass, "missing_credit_decisions": missing,
                        "maximum_credit_delay_seconds": maximum_delay,
                        "retained_credit_fraction": min(retained_fraction, 1.0),
                        "expired_credit_decisions": expired, "unknown_credit_decisions": missing - expired,
                        "credit_allocations": len(credits), "unassigned_reward": int(not credits),
                        "captured_tag_peak": float(peak)}


class ExperimentalPlasticity:
    def __init__(self, brain: Any, config: PlasticityConfig) -> None:
        self.config = config
        edges = len(brain.circuit["edges"])
        self.bank = EligibilityBank(edges, config)
        self.pre_trace = np.zeros(edges, dtype=np.float64)
        self.post_trace = np.zeros(edges, dtype=np.float64)
        self.tag = np.zeros(edges, dtype=np.float64)
        self.credit = np.zeros(edges, dtype=np.float64)
        self._dopamine = np.zeros(edges, dtype=np.float64)
        self._dan = np.zeros(len(brain.circuit["dan"]), dtype=np.float64)
        self._bin_metrics = np.zeros(BIN_METRICS, dtype=np.float64)
        self._no_stimulation = np.empty(0, dtype=np.int64)
        self.credit_metrics: dict[str, float | int] = {}
        self.admission_allowed = True
        self.reinforcement_gain = 1.0
        self._frame_at: datetime | None = None
        self._frame_finalized = False

    @property
    def buffer_nbytes(self) -> int:
        """Engine-owned NumPy bytes; excludes Python metadata and the full brain."""
        return sum(a.nbytes for a in (self.bank._values, self.bank._timestamps,
            self.bank._credit_slots, self.bank._credit_fractions, self.pre_trace,
            self.post_trace, self.tag, self.credit, self._dopamine, self._dan, self._bin_metrics))

    def begin_frame(self, now: datetime, credits: tuple[DecisionCredit, ...], *, has_reward: bool) -> None:
        now_us = _micros(now)
        if self.bank._last_decision is not None and now_us <= self.bank._last_decision:
            raise ValueError("eligibility decisions must strictly increase")
        if credits and not has_reward:
            raise ValueError("decision credit requires delivered reward evidence")
        # Validate/capture before advancing the ring clock. Expiry uses the same
        # integer boundary in both paths, so expired tags cannot be resurrected.
        self.credit_metrics = self.bank.capture_into(now, credits, self.credit)
        self.admission_allowed = self.bank.reserve(now)
        self.credit_metrics["unassigned_reward"] = int(has_reward and not credits)
        self.credit_metrics["tag_buffer_bytes"] = self.bank._values.nbytes
        self.credit_metrics["working_buffer_bytes"] = self.buffer_nbytes - self.bank._values.nbytes
        self.tag.fill(0)  # retain pre/post neural traces, tag only this decision's bins
        self._frame_at, self._frame_finalized = now, False

    def finalize_frame(self, now: datetime, side: str) -> bool:
        """Called once after fixed decoding, under the controller's session lock."""
        if (side not in ("BUY", "SELL", "HOLD") or self._frame_at is None
                or _micros(now) != _micros(self._frame_at) or self._frame_finalized):
            raise ValueError("current, unfinalized decoded frame required")
        allowed = side == "HOLD" or (self.admission_allowed and len(self.bank) < self.bank.capacity)
        if side != "HOLD" and allowed:
            self.bank.store(now, self.tag)
        else:
            # HOLD represents no order: release the reservation so a later store
            # at this instant cannot inject a HOLD tag into the ring.
            self.bank._reserved = None
        self._frame_finalized = True
        return allowed

    def clear_credit(self) -> None:
        self.bank.clear()
        self.credit.fill(0)
        self.tag.fill(0)
        self.credit_metrics = {}
        self._frame_at, self._frame_finalized = None, False

    def set_reinforcement(self, signal: float, stressed_return: float, booked_fee_return: float,
                          *, net_return: float) -> None:
        """Causal, bounded gain; losses remain losses in every objective metric."""
        if (not all(math.isfinite(v) for v in (signal, stressed_return, booked_fee_return, net_return))
                or not -1 <= signal <= 1 or booked_fee_return < 0):
            raise ValueError("finite causal reward and fee evidence required")
        cfg = self.config
        self.reinforcement_gain = float(reinforcement_gain_kernel(
            float(signal), float(net_return), float(stressed_return), float(booked_fee_return),
            float(cfg.positive_return_threshold), float(cfg.positive_gain), float(cfg.fee_shock_gain)))

    def advance(self, brain: Any, counts: Any, seconds: float, *, learning: bool,
                stimulation: Any, delta_limit: float) -> None:
        if brain.weights_frozen or not learning:
            return
        cfg = self.config
        u, w = brain.memory_u, brain.memory_w
        circuit = brain.circuit
        stimulated = self._no_stimulation if stimulation is None else stimulation[0]
        states = (u, w, self.credit, self.tag, self.pre_trace, self.post_trace, self._dopamine)
        indices = (circuit["edges"], circuit["pre"], circuit["dan"], brain.post, stimulated)
        gain = circuit["gain"]
        effective_eta = brain.eta * cfg.eta_scale * self.reinforcement_gain
        try:
            if (not 0 < seconds <= .0100001 or not 0 < delta_limit <= .1
                    or not 0 <= effective_eta <= 1
                    or any(not _vector(a, self.bank.edges, (np.float64,), writable=True) for a in states)
                    or any(np.shares_memory(a, b) for i, a in enumerate(states) for b in states[:i])
                    or any(not _vector(a, None, (np.int32, np.int64)) for a in indices)
                    or len(circuit["edges"]) != self.bank.edges or len(circuit["pre"]) != self.bank.edges
                    or len(circuit["dan"]) != len(self._dan)
                    or not _vector(counts, None, (np.int32, np.int64, np.float32, np.float64))
                    or not _vector(brain.dan_baseline_hz, len(self._dan), (np.float64,))
                    or not _vector(brain.baseline_plastic, self.bank.edges, (np.float32, np.float64))
                    or not _vector(brain.weight, len(brain.post), (np.float32, np.float64), writable=True)
                    or np.shares_memory(brain.weight, brain.baseline_plastic)
                    or not isinstance(gain, np.ndarray) or gain.shape != (len(self._dan), self.bank.edges)
                    or gain.dtype not in (np.float32, np.float64) or not gain.flags.c_contiguous):
                raise ValueError("invalid experimental plasticity array layout or parameters")
            plasticity_kernel(self.pre_trace, self.post_trace, self.tag, self.credit, u, w,
                counts, circuit["edges"], circuit["pre"], brain.post, circuit["dan"], gain,
                brain.dan_baseline_hz, stimulated, self._dan, self._dopamine, brain.weight,
                brain.baseline_plastic, self._bin_metrics, float(seconds), cfg.neural_trace_seconds,
                cfg.rate_scale_hz, cfg.dan_threshold_hz, float(effective_eta), cfg.homeostasis_per_second,
                cfg.saturation_decay_per_second, cfg.near_bound_fraction, delta_limit * (seconds / .01),
                cfg.maximum_homeostatic_loss_per_second,
                float(np.finfo(brain.weight.dtype).smallest_subnormal) * 10.,
                float(np.finfo(brain.weight.dtype).max))
        except (ValueError, IndexError):
            brain.plasticity_diagnostics["nonfinite_or_invalid_updates"] = int(
                brain.plasticity_diagnostics.get("nonfinite_or_invalid_updates", 0)) + 1
            raise
        saturation, rate, clipped, largest, absolute, erasure_rate, loss, requested, active, any_dan = self._bin_metrics
        metrics = brain.plasticity_diagnostics
        metrics["capture_active_bins"] = int(metrics.get("capture_active_bins", 0)) + int(active)
        metrics["boosted_capture_bins"] = int(metrics.get("boosted_capture_bins", 0)) + int(active and self.reinforcement_gain > 1)
        metrics["fee_damped_capture_bins"] = int(metrics.get("fee_damped_capture_bins", 0)) + int(active and self.reinforcement_gain < 1)
        metrics["dan_subthreshold_bins"] = int(metrics.get("dan_subthreshold_bins", 0)) + int(stimulation is not None and not any_dan)
        metrics["bins"] = int(metrics.get("bins", 0)) + 1
        metrics["near_saturation_peak"] = max(float(metrics.get("near_saturation_peak", 0)), float(saturation))
        metrics["near_saturation_seconds"] = float(metrics.get("near_saturation_seconds", 0)) + saturation * seconds
        metrics["observed_neural_seconds"] = float(metrics.get("observed_neural_seconds", 0)) + seconds
        metrics["homeostasis_rate"] = float(rate)
        metrics["trust_region_clipped"] = int(metrics.get("trust_region_clipped", 0)) + int(clipped)
        metrics["maximum_unbounded_delta"] = max(float(metrics.get("maximum_unbounded_delta", 0)), float(largest))
        metrics["absolute_saturation_seconds"] = float(metrics.get("absolute_saturation_seconds", 0)) + seconds * int(absolute > 0)
        metrics["absolute_saturation_edge_seconds"] = float(metrics.get("absolute_saturation_edge_seconds", 0)) + absolute * seconds
        metrics["severe_saturation_seconds"] = float(metrics.get("severe_saturation_seconds", 0)) + seconds * int(saturation >= .95)
        metrics["homeostatic_erasure_integral"] = float(metrics.get("homeostatic_erasure_integral", 0)) + erasure_rate * seconds
        metrics["maximum_bin_homeostatic_loss"] = max(float(metrics.get("maximum_bin_homeostatic_loss", 0)), loss)
        metrics["homeostasis_limited_bins"] = int(metrics.get("homeostasis_limited_bins", 0)) + int(requested > rate)
        metrics["effective_eta"] = effective_eta
        metrics["reinforcement_gain"] = self.reinforcement_gain
        metrics["dan_threshold_hz"] = cfg.dan_threshold_hz


def _vector(value: Any, length: int | None, dtypes: tuple[Any, ...], *, writable: bool = False) -> bool:
    """Check metadata without copying or constructing an edge-sized mask."""
    return (isinstance(value, np.ndarray) and value.ndim == 1
            and (length is None or len(value) == length) and value.dtype in dtypes
            and value.flags.c_contiguous and (not writable or value.flags.writeable))
