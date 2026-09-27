"""User-supplied goals and constraints, kept separate from measured evidence."""

import datetime as dt
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AthleteObservation(BaseModel):
    """Dated athlete testimony, distinct from aspirations and coaching preferences."""

    model_config = ConfigDict(extra="forbid")

    date: dt.date | None = None
    category: Literal["symptoms", "effort", "fueling", "equipment", "context"]
    observation: str = Field(min_length=1, max_length=4000)
    source: str = Field(min_length=1, max_length=1000)


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=5, max_length=4000)
    race_date: date | None = None
    distance_km: float | None = Field(default=None, gt=0, le=300)
    target_time_seconds: int | None = Field(default=None, gt=0, le=604800)
    max_stressors_per_week: int | None = Field(default=None, ge=0, le=7)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=20000)
    athlete_observations: list[AthleteObservation] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def consistent_target(self):
        if self.target_time_seconds is not None and self.distance_km is None:
            raise ValueError("A time target also needs a distance")
        if any(len(item) > 2000 for item in self.constraints):
            raise ValueError("Each constraint must be at most 2000 characters")
        return self


def parse_finish_time(value: str) -> int:
    pieces = value.split(":")
    if len(pieces) != 3 or not all(part.isdigit() for part in pieces):
        raise ValueError("Use HH:MM:SS, for example 03:30:00")
    hours, minutes, seconds = map(int, pieces)
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Minutes and seconds must be below 60")
    result = hours * 3600 + minutes * 60 + seconds
    if result <= 0:
        raise ValueError("Finish time must be positive")
    return result
