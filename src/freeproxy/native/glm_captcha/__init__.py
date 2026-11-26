"""Ported Aliyun device-token and captcha helpers (MIT, GLM-Free-API).

The original scripts live in the GLM-Free-API repository under `.assets/scripts`.
Only the network layer was adapted from `requests` to `httpx`; the crypto and
payload logic is kept verbatim so it stays compatible with the reverse-engineered
web client.
"""
