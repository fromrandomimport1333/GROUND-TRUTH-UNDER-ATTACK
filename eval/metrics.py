"""Evaluation-only binary metrics; labels must never enter detector inputs."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class LabeledDecision:
    sample_id: str
    is_attack: bool
    predicted_attack: bool


def binary_metrics(decisions, *, maximum: int = 1000000):
    if type(maximum) is not int or not 1 <= maximum <= 1000000:
        raise ValueError('invalid evaluation bound')
    seen = set()
    tp = tn = fp = fn = 0
    for item in decisions:
        if len(seen) >= maximum:
            raise BufferError('evaluation exceeds configured bound')
        if not isinstance(item, LabeledDecision) or type(item.is_attack) is not bool or type(item.predicted_attack) is not bool:
            raise TypeError('explicit boolean labels and decisions required')
        if type(item.sample_id) is not str or not 1 <= len(item.sample_id) <= 256 or item.sample_id in seen:
            raise ValueError('unique nonempty sample IDs required')
        seen.add(item.sample_id)
        if item.is_attack and item.predicted_attack: tp += 1
        elif item.is_attack: fn += 1
        elif item.predicted_attack: fp += 1
        else: tn += 1
    ratio = lambda n, d: n / d if d else None
    return {'samples': len(seen), 'true_positive': tp, 'true_negative': tn,
            'false_positive': fp, 'false_negative': fn,
            'detection_rate': ratio(tp, tp+fn), 'false_positive_rate': ratio(fp, fp+tn),
            'precision': ratio(tp, tp+fp), 'f1': ratio(2*tp, 2*tp+fp+fn),
            'accuracy': ratio(tp+tn, len(seen))}


def threshold_decisions(labels: dict[str, bool], scores: dict[str, float], *, threshold: float):
    """Caller chooses threshold on training/validation data, not this test set."""
    if len(labels)>1000000 or len(scores)>1000000:
        raise BufferError("evaluation exceeds configured bound")
    if set(labels) != set(scores):
        raise ValueError('label/score IDs must match exactly; missing predictions are not dropped')
    if type(threshold) is not float or not math.isfinite(threshold):
        raise ValueError('finite explicit threshold required')
    result = []
    for identity in sorted(labels):
        score = scores[identity]
        if type(score) is not float or not math.isfinite(score) or type(labels[identity]) is not bool:
            raise ValueError('finite float scores and boolean labels required')
        result.append(LabeledDecision(identity, labels[identity], score >= threshold))
    return tuple(result)
