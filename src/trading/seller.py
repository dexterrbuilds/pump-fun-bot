"""
Sell operations for pump.fun tokens.
"""

import struct
from typing import Final

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from src.core.client import SolanaClient
from src.core.curve import BondingCurveManager
from src.core.pubkeys import (
    LAMPORTS_PER_SOL,
    TOKEN_DECIMALS,
    PumpAddresses,
    SystemAddresses,
)
from src.core.wallet import Wallet
from src.trading.base import TokenInfo, Trader, TradeResult
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Discriminator for the sell instruction
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 12502976635542562355)


class TokenSeller(Trader):
    """Handles selling tokens on pump.fun."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        slippage: float = 0.25,
        max_retries: int = 5,
    ):
        """Initialize token seller.

        Args:
            client: Solana client for RPC calls
            wallet: Wallet for signing transactions
            curve_manager: Bonding curve manager
            slippage: Slippage tolerance (0.25 = 25%)
            max_retries: Maximum number of retry attempts
        """
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.slippage = slippage
        self.max_retries = max_retries

    async def execute(self, token_info: TokenInfo, *args, **kwargs) -> TradeResult:
        """Execute sell operation.

        Args:
            token_info: Token information

        Returns:
            TradeResult with sell outcome
        """
        try:
            # Extract token info
            mint = token_info.mint
            bonding_curve = token_info.bonding_curve
            associated_bonding_curve = token_info.associated_bonding_curve

            # Get associated token account
            associated_token_account = self.wallet.get_associated_token_address(mint)

            # Get token balance
            token_balance = await self.client.get_token_account_balance(
                associated_token_account
            )
            token_balance_decimal = token_balance / 10**TOKEN_DECIMALS

            logger.info(f"Token balance: {token_balance_decimal}")

            if token_balance == 0:
                logger.info("No tokens to sell.")
                return TradeResult(success=False, error_message="No tokens to sell")

            # Fetch token price
            curve_state = await self.curve_manager.get_curve_state(bonding_curve)
            token_price_sol = curve_state.calculate_price()

            logger.info(f"Price per Token: {token_price_sol:.8f} SOL")

            # Calculate minimum SOL output with slippage
            amount = token_balance
            expected_sol_output = float(token_balance_decimal) * float(token_price_sol)
            slippage_factor = 1 - self.slippage
            min_sol_output = int(
                (expected_sol_output * slippage_factor) * LAMPORTS_PER_SOL
            )

            logger.info(f"Selling {token_balance_decimal} tokens")
            logger.info(f"Expected SOL output: {expected_sol_output:.8f} SOL")
            logger.info(
                f"Minimum SOL output (with {self.slippage * 100}% slippage): {min_sol_output / LAMPORTS_PER_SOL:.8f} SOL"
            )

            tx_signature = await self._send_sell_transaction(
                mint,
                bonding_curve,
                associated_bonding_curve,
                associated_token_account,
                amount,
                min_sol_output,
            )

            success = await self.client.confirm_transaction(tx_signature)

            if success:
                logger.info(f"Sell transaction confirmed: {tx_signature}")
                return TradeResult(
                    success=True,
                    tx_signature=tx_signature,
                    amount=token_balance_decimal,
                    price=token_price_sol,
                )
            else:
                return TradeResult(
                    success=False,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            logger.error(f"Sell operation failed: {str(e)}")
            return TradeResult(success=False, error_message=str(e))

    async def _send_sell_transaction(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        associated_bonding_curve: Pubkey,
        associated_token_account: Pubkey,
        token_amount: int,
        min_sol_output: int,
    ) -> str:
        """Send sell transaction.

        Args:
            mint: Token mint
            bonding_curve: Bonding curve address
            associated_bonding_curve: Associated bonding curve address
            associated_token_account: User's token account
            token_amount: Amount of tokens to sell in raw units
            min_sol_output: Minimum SOL to receive in lamports

        Returns:
            Transaction signature

        Raises:
            Exception: If transaction fails after all retries
        """
        # Prepare sell instruction accounts
        accounts = [
            AccountMeta(
                pubkey=PumpAddresses.GLOBAL, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PumpAddresses.FEE, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=associated_bonding_curve, is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=associated_token_account, is_signer=False, is_writable=True
            ),
            AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
            AccountMeta(
                pubkey=SystemAddresses.PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=SystemAddresses.TOKEN_PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=PumpAddresses.PROGRAM, is_signer=False, is_writable=False
            ),
        ]

        # Prepare sell instruction data
        data = (
            EXPECTED_DISCRIMINATOR
            + struct.pack("<Q", token_amount)
            + struct.pack("<Q", min_sol_output)
        )
        sell_ix = Instruction(PumpAddresses.PROGRAM, data, accounts)

        # Prepare sell transaction data
        recent_blockhash: Hash = await self.client.get_latest_blockhash()
        sell_message = Message([sell_ix], self.wallet.keypair.pubkey())
        sell_tx = Transaction([self.wallet.keypair], sell_message, recent_blockhash)

        try:
            return await self.client.send_transaction(
                sell_tx,
                skip_preflight=True,
                max_retries=self.max_retries,
            )
        except Exception as e:
            logger.error(f"Sell transaction failed: {str(e)}")
            raise
