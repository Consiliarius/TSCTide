"""Moorwatch — the on-board companion readout for TSCTide.

A small always-on display for the boat netbook, answering the question the ICS
feed cannot: *right now, how much water is over the mooring, and how long have I
got?* Computes entirely offline from the bundled harmonic model.

This package is a READ-ONLY consumer of the model. It imports app.harmonic,
app.secondary_port, app.access_calc and app.window_display and calls them
unchanged; it touches no database, opens no socket, and writes nothing back to
TSCTide. Everything it needs comes from those modules plus a local config.json.

It is deliberately NOT part of the Docker image (the Dockerfile copies only
app/ and scripts/). The netbook runs it from a checkout:

    python3 -m moorwatch          # one-shot CLI readout
    python3 -m moorwatch --watch  # refreshing console readout

See moorwatch/README.md for deployment and the config sync.
"""
