from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TypedDict


class BasicParseResult(TypedDict, total=False):
    price_on_request: bool
    price: int
    year: int
    power: str
    mileage: str
    vendor: str
    seller_name: str
    seller_phone: str
    seller_email: str
    seller_profile_url: str


class AIAnalysisResult(TypedDict, total=False):
    category: str
    vendor: str
    model: str
    price: int
    currency: str
    monthly_payment: int
    year: int
    condition: str
    location: str
    specs: dict
    pros: list[str]
    cons: list[str]
    analogs_mentioned: list[str]
    seller_name: str
    seller_phone: str
    seller_email: str
    seller_profile_url: str


class AnalogReview(TypedDict, total=False):
    pros: list[str]
    cons: list[str]
    price_hint: Optional[int]
    note: str
    best_link: Optional[str]


class ValidationResult(TypedDict, total=False):
    is_valid: bool
    comment: str


class SearchResult(TypedDict):
    title: str
    link: str
    snippet: str


class ListingSummary(TypedDict):
    title: str
    link: str
    snippet: str
    price_guess: Optional[int]


class SonarAnalogResult(TypedDict, total=False):
    name: str
    description: str
    price_range: str
    key_difference: str


class SonarComparisonResult(TypedDict, total=False):
    winner: str
    original_advantages: list[str]
    original_disadvantages: list[str]
    analog_advantages: list[str]
    analog_disadvantages: list[str]
    recommendation: str
    price_diff: str
    price_verdict: str
    original_url: str
    original_title: str
    original_price: Optional[int]
    analog_url: str
    analog_title: str
    analog_price: Optional[int]
    sonar_comparison: bool


class UserInput(TypedDict):
    item: str
    client_price: Optional[int]
    use_ai: bool
    num_results: int
    memory_context: Optional[str]



@dataclass
class LeasingOffer:
    title: str
    url: str
    source: str
    model: str = ""
    price: Optional[int] = None
    price_str: Optional[str] = None
    monthly_payment: Optional[int] = None
    monthly_payment_str: Optional[str] = None
    price_on_request: bool = False
    year: Optional[int] = None
    power: Optional[str] = None
    mileage: Optional[str] = None
    vendor: Optional[str] = None
    condition: Optional[str] = None
    location: Optional[str] = None
    seller_name: Optional[str] = None
    seller_phone: Optional[str] = None
    seller_email: Optional[str] = None
    seller_profile_url: Optional[str] = None
    specs: dict = field(default_factory=dict)
    category: Optional[str] = None
    currency: Optional[str] = None
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    analogs: list[str] = field(default_factory=list)
    analogs_suggested: list[str] = field(default_factory=list)

    def has_data(self) -> bool:
        return any([self.price is not None, self.monthly_payment is not None, self.price_on_request])
