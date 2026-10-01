from arb.scrapers.admiral import AdmiralScraper
from arb.scrapers.betby import (
    BCGameScraper,
    BetFuryScraper,
    BetpandaScraper,
    BetplayScraper,
    FlushScraper,
    GoldenPandaScraper,
    RainbetScraper,
    ThrillScraper,
)
from arb.scrapers.casinok import CasinoKScraper
from arb.scrapers.cloudbet import CloudbetScraper
from arb.scrapers.dexsport import DexsportScraper
from arb.scrapers.duelbits import DuelbitsScraper
from arb.scrapers.polymarket import PolymarketScraper
from arb.scrapers.sxbet import SXBetScraper
from arb.scrapers.shuffle import ShuffleScraper
from arb.scrapers.vave import VaveScraper
from arb.scrapers.meridian import MeridianScraper
from arb.scrapers.mozzart import MozzartScraper
from arb.scrapers.restapi import (
    BetOleScraper,
    BrazilBetScraper,
    Kladionica365Scraper,
    MaxBetScraper,
    MerkurXTipScraper,
    OktagonScraper,
    SoccerbetScraper,
)
from arb.scrapers.king import KingScraper
from arb.scrapers.nsoft import BalkanbetScraper
from arb.scrapers.onexbet import OneXBetScraper, OneXBitScraper, VivatBetScraper
from arb.scrapers.sportsbet import SportsbetScraper
from arb.scrapers.stake import StakeScraper
from arb.scrapers.starbet import StarBetScraper
from arb.scrapers.superbet import SuperbetScraper
from arb.scrapers.topbet import TopbetScraper
from arb.scrapers.wild import WildScraper

ALL_SCRAPERS = [
    MozzartScraper,
    MeridianScraper,
    MaxBetScraper,
    AdmiralScraper,
    SoccerbetScraper,
    MerkurXTipScraper,
    OktagonScraper,
    BetOleScraper,
    BrazilBetScraper,
    SuperbetScraper,
    BalkanbetScraper,
    Kladionica365Scraper,
    StarBetScraper,
    OneXBetScraper,
    VivatBetScraper,
    TopbetScraper,
    KingScraper,
    # crypto sportsbooks ("🪙 Prebaci na kripto" in the bot)
    StakeScraper,
    OneXBitScraper,
    BCGameScraper,
    BetFuryScraper,
    RainbetScraper,
    BetpandaScraper,
    WildScraper,
    SportsbetScraper,
    BetplayScraper,
    GoldenPandaScraper,
    CasinoKScraper,
    ThrillScraper,
    CloudbetScraper,
    DexsportScraper,
    DuelbitsScraper,
    ShuffleScraper,
    VaveScraper,
    FlushScraper,
    # exchanges: lowest margins, best odds
    PolymarketScraper,
    SXBetScraper,
]
