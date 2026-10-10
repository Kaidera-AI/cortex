"""Fixed offered load over eight persistent, cooperatively cancellable clients."""
import asyncio
from dataclasses import asdict, dataclass
import math
import time


@dataclass(frozen=True)
class RunConfig:
    duration_seconds: float = 300
    warmup_seconds: float = 60
    deadline_seconds: float = 10
    rps: int = 40
    clients: int = 8

    def validate(self):
        if type(self.clients) is not int or self.clients != 8 or type(self.rps) is not int or self.rps != 40:
            raise ValueError("eight clients and40RPS required")
        for value, zero_allowed in [(self.duration_seconds, False), (self.warmup_seconds, True),
                                    (self.deadline_seconds, False)]:
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or value < 0 or (not zero_allowed and value == 0)):
                raise ValueError("finite explicit intervals required")
        for value in [self.duration_seconds, self.warmup_seconds]:
            if not math.isclose(value * self.rps, round(value * self.rps), abs_tol=1e-9, rel_tol=0):
                raise ValueError("interval must contain an integral offered count")


class Clock:
    now = staticmethod(time.monotonic)
    sleep = staticmethod(asyncio.sleep)


async def run(queries, factory, config, *, clock=None, observer=None):
    """Factory constructs without I/O; each tracked client exposes open/request/aclose.

    Queue deadlines use scheduled arrival, and the scheduler never awaits a request.
    Indefinitely blocking or cancellation-suppressing ports are outside this contract.
    """
    config.validate()
    if (not queries or any(not isinstance(q, dict) or not isinstance(q.get("id"), str) or not q["id"]
                           for q in queries) or len({q["id"] for q in queries}) != len(queries)):
        raise ValueError("unique frozen query IDs required")
    clock = clock or Clock()
    clients, observations, observation_errors, close_errors = [], [], [], []
    opened = closed = 0
    collector = None
    result = None

    async def observe():
        if observer is None:
            return
        try:
            value = await observer()
            if not isinstance(value, dict):
                raise ValueError("observer must return a scoped mapping")
            observations.append({"observed_at": clock.now(), "value": value})
        except Exception as error:
            observation_errors.append(type(error).__name__)

    async def collect():
        while True:
            await asyncio.sleep(1)
            await observe()

    async def phase(duration, name):
        origin = clock.now()
        records = []
        queues = [asyncio.Queue() for _ in clients]

        async def worker(number):
            while True:
                offer = await queues[number].get()
                if offer is None:
                    return
                row, query = offer
                row["started_at"] = clock.now()
                row["queue_seconds"] = max(0., row["started_at"] - row["scheduled_at"])
                remaining = row["scheduled_at"] + config.deadline_seconds - clock.now()
                try:
                    if remaining <= 0:
                        raise TimeoutError()
                    response = await asyncio.wait_for(clients[number].request(query), remaining)
                    row["completed_at"] = clock.now()
                    row["latency_seconds"] = row["completed_at"] - row["scheduled_at"]
                    if row["latency_seconds"] >= config.deadline_seconds:
                        row["status"] = "TIMEOUT"
                    else:
                        row.update(status="OK", response=response)
                except TimeoutError:
                    row.update(status="TIMEOUT", completed_at=clock.now(),
                               latency_seconds=max(config.deadline_seconds, clock.now() - row["scheduled_at"]))
                except Exception as error:
                    row.update(status="ERROR", error_class=type(error).__name__, completed_at=clock.now(),
                               response_elapsed_seconds=max(0., clock.now() - row["scheduled_at"]),
                               latency_seconds=max(config.deadline_seconds, clock.now() - row["scheduled_at"]))
                records.append(row)

        workers = [asyncio.create_task(worker(i)) for i in range(len(clients))]
        try:
            for index in range(round(duration * config.rps)):
                scheduled = origin + index / config.rps
                delay = scheduled - clock.now()
                if delay > 0:
                    await clock.sleep(delay)
                query = queries[index % len(queries)]
                row = {"offer_id": index, "client": index % config.clients, "query_id": query["id"],
                       "phase": name, "scheduled_at": scheduled,
                       "scheduled_offset_seconds": scheduled - origin, "dispatched_at": clock.now()}
                if clock.now() - scheduled >= 1 / config.rps:
                    row.update(status="MISSED", started_at=None, completed_at=clock.now(), queue_seconds=None,
                               latency_seconds=max(config.deadline_seconds, clock.now() - scheduled))
                    records.append(row)
                else:
                    queues[row["client"]].put_nowait((row, query))
            for queue in queues:
                queue.put_nowait(None)
            if origin + duration > clock.now():
                await clock.sleep(origin + duration - clock.now())
            await asyncio.gather(*workers)
            return sorted(records, key=lambda row: row["offer_id"]), origin, clock.now()
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    try:
        for number in range(config.clients):
            client = factory(number)
            clients.append(client)  # Track the pending connection before open's external effect.
            await asyncio.wait_for(client.open(), config.deadline_seconds)
            opened += 1
        await observe()
        if observer is not None:
            collector = asyncio.create_task(collect())
        warmup, _, _ = await phase(config.warmup_seconds, "warmup")
        measured, start, end = await phase(config.duration_seconds, "measured")
        await observe()
        result = {"config": asdict(config), "records": measured, "warmup_records": warmup,
                  "measured_start": start, "measured_end": end}
    finally:
        if collector is not None:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
        for client in reversed(clients):
            try:
                await asyncio.wait_for(client.aclose(), config.deadline_seconds)
                closed += 1
            except (Exception, asyncio.CancelledError) as error:
                close_errors.append(type(error).__name__)
    result.update(clients_opened=opened, clients_closed=closed, close_errors=close_errors,
                  observations=observations, observation_errors=observation_errors)
    return result
