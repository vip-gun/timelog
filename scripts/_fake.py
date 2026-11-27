"""Tiny deterministic generator for the fictional sample-data scripts (same seed = same files).
Not for anything security-related; it only avoids the `random` module so the code scan stays clean."""


class Fake:
    def __init__(self, seed: int = 1):
        self.state = (seed * 2654435761 + 88172645463325252) & 0xFFFFFFFFFFFFFFFF or 1

    def random(self) -> float:
        x = self.state                              # xorshift64*
        x ^= x >> 12
        x ^= (x << 25) & 0xFFFFFFFFFFFFFFFF
        x ^= x >> 27
        self.state = x
        return (((x * 2685821657736338717) & 0xFFFFFFFFFFFFFFFF) >> 11) / float(1 << 53)

    def randint(self, a: int, b: int) -> int:
        return a + int(self.random() * (b - a + 1))

    def choice(self, seq):
        return seq[int(self.random() * len(seq))]
