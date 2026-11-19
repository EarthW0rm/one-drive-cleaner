# SPDX-License-Identifier: GPL-3.0-only
"""Testes dos pontos de parada do Graph fake: contagem, fases, liberação e derrubada."""

import threading

import pytest

from tests.fakes.breakpoints import Breakpoints, Phase


def test_nth_must_be_positive() -> None:
    with pytest.raises(ValueError, match="nth"):
        Breakpoints().add("move", 0)


def test_arrivals_are_numbered_per_key() -> None:
    gates = Breakpoints()

    assert [gates.arrive("move"), gates.arrive("move"), gates.arrive("delta")] == [1, 2, 1]


def test_counting_starts_when_the_point_is_registered() -> None:
    gates = Breakpoints()
    gates.arrive("move")
    gates.arrive("move")
    point = gates.add("move", 2)

    assert gates.find("move", 3, Phase.BEFORE) is None
    assert gates.find("move", 4, Phase.BEFORE) is point


def test_find_matches_key_number_and_phase_only() -> None:
    gates = Breakpoints()
    point = gates.add("move", 1, Phase.AFTER)

    assert gates.find("move", 1, Phase.AFTER) is point
    assert gates.find("move", 1, Phase.BEFORE) is None
    assert gates.find("delta", 1, Phase.AFTER) is None


@pytest.mark.asyncio
async def test_pause_without_a_point_returns_immediately() -> None:
    assert await Breakpoints().pause("move", 1, Phase.BEFORE) is None


@pytest.mark.asyncio
async def test_pause_holds_until_released() -> None:
    gates = Breakpoints()
    point = gates.add("move", 1)
    held = threading.Event()

    def controller() -> None:
        assert point.wait_reached(10)
        held.set()
        point.release()

    thread = threading.Thread(target=controller)
    thread.start()
    paused = await gates.pause("move", gates.arrive("move"), Phase.BEFORE)
    thread.join(10)

    assert held.is_set()
    assert paused is point
    assert point.dropped is False


@pytest.mark.asyncio
async def test_drop_is_visible_to_the_server_side() -> None:
    gates = Breakpoints()
    point = gates.add("delta", 1, Phase.MID_BODY)

    def controller() -> None:
        assert point.wait_reached(10)
        point.drop()

    thread = threading.Thread(target=controller)
    thread.start()
    paused = await gates.pause("delta", gates.arrive("delta"), Phase.MID_BODY)
    thread.join(10)

    assert paused is point
    assert point.dropped is True


@pytest.mark.asyncio
async def test_release_all_frees_every_held_request() -> None:
    gates = Breakpoints()
    first = gates.add("move", 1)
    second = gates.add("delta", 1)
    gates.release_all()

    assert await gates.pause("move", gates.arrive("move"), Phase.BEFORE) is first
    assert await gates.pause("delta", gates.arrive("delta"), Phase.BEFORE) is second


def test_wait_reached_times_out_when_the_request_never_arrives() -> None:
    assert Breakpoints().add("move", 1).wait_reached(0.01) is False
