from typing import Dict, Any, Tuple


def calculate_grade(total_score: float) -> str:
    if total_score >= 90:
        return "S"
    elif total_score >= 80:
        return "A"
    elif total_score >= 70:
        return "B"
    elif total_score >= 60:
        return "C"
    else:
        return "D"


def calculate_score(metrics: Dict[str, Any], weight_config: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    score_details = {}
    total_score = 0.0

    dimensions = weight_config.get("dimensions", {})

    for dim_key, dim_config in dimensions.items():
        dim_weight = dim_config.get("weight", 0)
        dim_metrics = dim_config.get("metrics", {})

        dim_score = 0.0
        dim_metric_scores = {}

        for metric_key, metric_config in dim_metrics.items():
            metric_weight = metric_config.get("weight", 0)
            metric_value = metrics.get(metric_key, 0)
            metric_full_score = metric_config.get("full_score", 100)

            if isinstance(metric_value, (int, float)):
                if metric_config.get("type") == "higher_is_better":
                    raw_score = min(metric_value / metric_config.get("baseline", 1) * metric_full_score, metric_full_score)
                elif metric_config.get("type") == "lower_is_better":
                    baseline = metric_config.get("baseline", 1)
                    if metric_value <= 0:
                        raw_score = metric_full_score
                    else:
                        raw_score = min(baseline / metric_value * metric_full_score, metric_full_score)
                else:
                    raw_score = min(metric_value, metric_full_score)
            else:
                raw_score = 0

            weighted_score = raw_score * (metric_weight / 100)
            dim_metric_scores[metric_key] = {
                "value": metric_value,
                "raw_score": round(raw_score, 2),
                "weight": metric_weight,
                "weighted_score": round(weighted_score, 2),
            }
            dim_score += weighted_score

        dim_total_weighted = dim_score * (dim_weight / 100)
        score_details[dim_key] = {
            "weight": dim_weight,
            "dimension_score": round(dim_score, 2),
            "weighted_score": round(dim_total_weighted, 2),
            "metrics": dim_metric_scores,
        }
        total_score += dim_total_weighted

    total_score = round(total_score, 2)
    grade = calculate_grade(total_score)
    score_details["total_score"] = total_score
    score_details["grade"] = grade

    return total_score, score_details
