import colorsys

from textual.color import Color
from textual.reactive import var, reactive
from textual.widgets import Static


class Stat(Static):
    name = var(None)
    value = reactive(0)

    periodic_update = var(False)
    update_frequency = var(1)

    async def on_mount(self):
        if self.periodic_update:
            self.set_interval(self.update_frequency, self.update_value)
        else:
            await self.update_value()

    async def update_value(self):
        raise NotImplementedError(
            "a Stat subclass implements update_value and sets self.value"
        )

    def render(self):
        return f"{self.name}: {self.value}"


class ColorStat(Stat):
    """Change the color between red and green as a function of the value."""

    SATURATION = 1.0
    LIGHTNESS = 0.5

    # The 100 percentage units cover the hue from 0.0 (red) to 0.3 (green).
    HSL_STEP = 0.003

    # normal: a low value is green and a high value is red, as for the CPU usage.
    # inverse: a low value is red and a high value is green, as for the free disk
    # space.
    inverse = var(False)
    percentage = reactive(0)

    def watch_percentage(self):
        pct = max(0.0, min(100.0, float(self.percentage)))
        position = pct if self.inverse else 100 - pct
        hue = position * self.HSL_STEP
        rgb = colorsys.hls_to_rgb(hue, self.LIGHTNESS, self.SATURATION)
        new_color = Color(*(int(v * 255) for v in rgb))

        if self.styles.color != new_color:
            self.styles.color = new_color
