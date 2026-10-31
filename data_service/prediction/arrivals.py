"""Arrival-rate profiles and exact integrals, in minutes and people/minute.

Smooth profiles use monotone cubic Hermite segments: weighted harmonic interior
tangents and zero end tangents. Each segment stays within its endpoint rates;
the constant tail joins with zero slope. The editor uses the same coefficients.
"""
from bisect import bisect_right


class ArrivalProfile:
    def __init__(self, settings, duration):
        self.duration = duration
        self.mode = settings.get('arrival_interpolation', 'step')
        rows = settings.get('arrival_schedule') or [{'minute': 0, 'rate': settings['arrival_rate_per_minute']}]
        tangents = [0.] * len(rows)
        widths = [right['minute'] - left['minute'] for left, right in zip(rows, rows[1:])]
        secants = [(right['rate'] - left['rate']) / width for left, right, width in zip(rows, rows[1:], widths)]
        if self.mode == 'smooth':
            for i in range(1, len(rows) - 1):
                before, after = secants[i - 1], secants[i]
                if before * after > 0:
                    w1, w2 = 2 * widths[i] + widths[i - 1], widths[i] + 2 * widths[i - 1]
                    tangents[i] = (w1 + w2) / (w1 / before + w2 / after)
        self.segments, self.starts, self.prefix = [], [], [0.]
        for i, row in enumerate(rows):
            end = rows[i + 1]['minute'] if i + 1 < len(rows) else duration
            h = end - row['minute']
            if h <= 0:
                continue
            a = b = c = 0.
            if self.mode == 'smooth' and i + 1 < len(rows):
                delta = rows[i + 1]['rate'] - row['rate']
                c = h * tangents[i]
                a = -2 * delta + h * (tangents[i] + tangents[i + 1])
                b = 3 * delta - h * (2 * tangents[i] + tangents[i + 1])
            self.starts.append(row['minute'])
            self.segments.append((h, a, b, c, row['rate']))
            self.prefix.append(self.prefix[-1] + h * (a / 4 + b / 3 + c / 2 + row['rate']))

    def cumulative(self, minute):
        minute = min(self.duration, max(0., minute))
        if minute >= self.duration:
            return self.prefix[-1]
        i = max(0, bisect_right(self.starts, minute) - 1)
        h, a, b, c, d = self.segments[i]
        u = (minute - self.starts[i]) / h
        return self.prefix[i] + h * (a * u**4 / 4 + b * u**3 / 3 + c * u**2 / 2 + d * u)

    def integral(self, start, end):
        return max(0., self.cumulative(end) - self.cumulative(start))
