"""搜推广告 + 电商混排：召回 → 精排 → oCPX → pacing → Mixer。"""

__version__ = "0.1.0"

from .bid_controller import BidController, CampaignBidState
from .budget import BudgetTracker, CampaignBudget
from .commerce import CommerceEngine
from .pipeline import AdsEngine

__all__ = [
    "AdsEngine",
    "BidController",
    "BudgetTracker",
    "CampaignBidState",
    "CampaignBudget",
    "CommerceEngine",
    "__version__",
]
