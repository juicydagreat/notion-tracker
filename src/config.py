import os
from dotenv import load_dotenv

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC_URL = os.getenv(
    "HELIUS_RPC_URL",
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
)
WALLETS_FILE = os.getenv("WALLETS_FILE", "wallets.json")
DB_PATH = os.getenv("DB_PATH", "discovery.db")
MAX_CREDITS_PER_RUN = int(os.getenv("MAX_CREDITS_PER_RUN", "500"))

# Solana default fee in lamports - matches this fee = low confidence
DEFAULT_FEE_LAMPORTS = 5000

# Minimum co-occurrence count to flag as candidate
MIN_CO_OCCURRENCE = 2

# How many recent transactions to pull per wallet for analysis
TX_LOOKBACK_LIMIT = 100
