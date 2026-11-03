"""Monte-Carlo of the alert-count rule against Layer 3's measured 2024 alert behaviour.
Nominal: per stream false-episode rate 0.0132/stream-day (=48 ticks), mean length 2.3 ticks; 3 streams/sensor.
Attack: duration in {12,48,96} ticks, each tick alerted with prob 0.33 (2024 point recall 32-35 %)."""
import numpy as np
rng = np.random.default_rng(0)
TICKS_MONTH = 48*30

def nominal(n_ticks):
    a = np.zeros(n_ticks, bool)
    for _ in range(3):
        starts = np.flatnonzero(rng.random(n_ticks) < 0.0132/48)
        for s in starts:
            a[s:s+rng.geometric(1/2.3)] = True
    return a

def first_cross(a, window, k):
    c = np.convolve(a.astype(int), np.ones(window, int))[:len(a)]
    hit = np.flatnonzero(c >= k)
    return hit[0] if len(hit) else None

def run(sw, sk, qw, qk, months=2000, attacks=3000):
    a = nominal(TICKS_MONTH*months)
    c_s = np.convolve(a.astype(int), np.ones(sw, int))[:len(a)] >= sk
    c_q = np.convolve(a.astype(int), np.ones(qw, int))[:len(a)] >= qk
    fs = np.sum(c_s[1:] & ~c_s[:-1]) / months
    fq = np.sum(c_q[1:] & ~c_q[:-1]) / months
    res = {}
    for d in (12, 48, 96):
        q = s = 0; tq = []
        for _ in range(attacks // 3):
            x = rng.random(d + qw) < 0.33; x[d:] = False      # alerts only during the attack
            if first_cross(x, sw, sk) is not None: s += 1
            t = first_cross(x, qw, qk)
            if t is not None: q += 1; tq.append(t)
        res[d] = (s/(attacks//3), q/(attacks//3), np.median(tq) if tq else None)
    return fs, fq, res

print("rule (susp k/win, quar k/win) | false susp/sensor-month | false quar/sensor-month | per duration: P(susp), P(quar), median ticks to quar")
for sw, sk, qw, qk in [(2,2,4,4), (12,3,24,6), (12,4,24,6), (12,3,24,7), (12,3,24,8), (12,4,24,8), (12,3,48,10), (12,4,48,10)]:
    fs, fq, res = run(sw, sk, qw, qk)
    print(f"{sk}/{sw:<2} {qk}/{qw:<2} | {fs:6.3f} | {fq:7.4f} | " + "  ".join(f"{d}:{r[0]:.2f},{r[1]:.2f},{r[2]}" for d, r in res.items()))
