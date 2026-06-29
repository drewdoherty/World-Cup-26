"""Google Sheets integration for the WCA bet ledger.

The sheet is the live view and edit surface for open/closed bets.
wca.db (on the mini) remains the authoritative backend; this module
syncs between the two, with the sheet winning on manual edits.
"""
