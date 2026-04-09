import os
from dotenv import load_dotenv

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC_URL = os.getenv(
    "HELIUS_RPC_URL",
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
)

# Free public Solana RPC — used for getSignaturesForAddress and getTransaction
# so Helius credits are only spent on getBlock (expensive, rare, cached).
# Alternatives: https://rpc.ankr.com/solana  |  https://solana-mainnet.g.alchemy.com/v2/demo
FREE_RPC_URL = os.getenv(
    "FREE_RPC_URL",
    "https://api.mainnet-beta.solana.com"
)

WALLETS_FILE = os.getenv("WALLETS_FILE", "wallets.json")
DB_PATH = os.getenv("DB_PATH", "discovery.db")
MAX_CREDITS_PER_RUN = int(os.getenv("MAX_CREDITS_PER_RUN", "500"))

# Solana transaction fee constants
# Base fee (5,000 lamports) + common default priority fee (1,000,000 lamports)
# = 1,005,000 lamports (0.001005 SOL) — what most wallets/bots pay by default.
# Transactions with this fee are low-confidence matches; anything different is
# more distinctive and worth scanning.
DEFAULT_FEE_LAMPORTS = 1_005_000

# Minimum co-occurrence count to flag as candidate
MIN_CO_OCCURRENCE = 2

# How many recent transactions to pull per wallet for analysis
TX_LOOKBACK_LIMIT = 100
