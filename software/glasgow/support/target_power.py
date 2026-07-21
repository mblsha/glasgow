import asyncio


__all__ = ["TargetPowerLifecycle"]


class TargetPowerLifecycle:
    """Debounce a target rail and run suspend/resume callbacks.

    The callbacks are deliberately independent of any applet interface. Applets that retain
    protocol state across target resets can share this policy without duplicating hysteresis,
    exception handling, and debounce behavior.
    """

    def __init__(self, logger, *, sample_voltage, is_active, suspend, resume,
            after_suspend=None, off_threshold=1.0, on_threshold=1.8,
            interval=0.25, debounce_samples=3, description="target power"):
        if not 0 <= off_threshold < on_threshold:
            raise ValueError("power thresholds must satisfy 0 <= off < on")
        if interval <= 0 or debounce_samples < 1:
            raise ValueError("power polling interval and debounce count must be positive")

        self._logger = logger
        self._sample_voltage = sample_voltage
        self._is_active = is_active
        self._suspend = suspend
        self._resume = resume
        self._after_suspend = after_suspend
        self._off_threshold = off_threshold
        self._on_threshold = on_threshold
        self._interval = interval
        self._debounce_samples = debounce_samples
        self._description = description

    async def run(self):
        below_count = above_count = 0
        self._logger.info(
            "monitoring %s (off below %.2f V, on above %.2f V)",
            self._description, self._off_threshold, self._on_threshold)
        while True:
            try:
                voltage = await self._sample_voltage()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._logger.warning("cannot sample %s: %s", self._description, error)
                await asyncio.sleep(self._interval)
                continue

            if self._is_active():
                below_count = below_count + 1 if voltage < self._off_threshold else 0
                above_count = 0
                if below_count >= self._debounce_samples:
                    await self._suspend(voltage)
                    if self._after_suspend is not None:
                        await self._after_suspend()
                    below_count = 0
            else:
                above_count = above_count + 1 if voltage > self._on_threshold else 0
                below_count = 0
                if above_count >= self._debounce_samples:
                    await self._resume(voltage)
                    above_count = 0
            await asyncio.sleep(self._interval)
