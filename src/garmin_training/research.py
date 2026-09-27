"""Bounded research context, separate from an athlete's observations."""

RESEARCH_CONTEXT = {
    "reviewed_at": "2026-09-27",
    "interpretation": (
        "General research informs coaching judgment. These summaries do not validate an "
        "individual dose, infer a personal causal effect, or supply a finish prediction. "
        "Cite the relevant URL when relying on a research finding. Distinguish original "
        "observational evidence from research synthesis and expert consensus."
    ),
    "sources": [
        {
            "id": "sleep-consensus-2021",
            "title": "Sleep and the athlete: narrative review and 2021 expert consensus recommendations",
            "url": "https://pubmed.ncbi.nlm.nih.gov/33144349/",
            "kind": "expert_consensus_and_narrative_review",
            "finding": (
                "The consensus recommends an individualized approach to sleep. It identifies "
                "training, travel and competition among influences on sleep, and emphasizes "
                "limitations of existing performance studies. Effects of partial restriction "
                "over one to three nights remain less certain than those of sleep deprivation."
            ),
            "application_limit": (
                "An athlete's observational sleep/performance association is not a causal "
                "effect. Do not derive a universal nightly requirement or cancel training "
                "from one watch sleep score."
            ),
        },
        {
            "id": "marathon-training-cohort-2020",
            "title": "Training for a (half-)marathon: Training volume and longest endurance run related to performance and running injuries",
            "url": "https://pubmed.ncbi.nlm.nih.gov/32421886/",
            "kind": "observational_cohort",
            "finding": (
                "Questionnaire data from 556 half-marathon and 441 marathon participants "
                "linked training volume and longest endurance-run categories with race "
                "performance. Among marathon runners, lower weekly volume and a longest "
                "run below 25 km were associated with slower finishes."
            ),
            "application_limit": (
                "Group associations do not prove that increasing an individual's distance "
                "now will improve their result, identify an optimal peak, or validate a "
                "late training jump. They support considering endurance development, not "
                "treating prior longest distance as a physiological ceiling."
            ),
        },
        {
            "id": "endurance-taper-meta-2023",
            "title": "Effects of tapering on performance in endurance athletes: A systematic review and meta-analysis",
            "url": "https://pubmed.ncbi.nlm.nih.gov/37163550/",
            "kind": "systematic_review_and_meta_analysis",
            "finding": (
                "Across 14 studies, tapering improved time-trial and time-to-exhaustion "
                "performance. Volume reductions with maintained intensity and frequency "
                "were among effective strategies; multiple taper durations showed benefits."
            ),
            "application_limit": (
                "This synthesis does not establish one athlete's optimal taper length or "
                "percentage, nor justify pre-taper overload merely because subgroup "
                "results favored it. Explain the selected taper in relation to achieved "
                "and proposed training. A taper is not a blanket cessation of training."
            ),
        },
    ],
}
