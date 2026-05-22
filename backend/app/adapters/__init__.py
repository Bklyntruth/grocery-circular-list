from .flipp import FlippAdapter
from .foodway import FoodwayAdapter
from .keyfood import KeyFoodAdapter
from .registry import register
from .shoprite import ShopRiteAdapter

# Single-store / brand-specific adapters first so they win over the broad Flipp matcher
register(FoodwayAdapter())
register(KeyFoodAdapter())
register(ShopRiteAdapter())
register(FlippAdapter())
