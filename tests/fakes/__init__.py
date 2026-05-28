"""In-process fakes for network.py unit testing.

These fakes satisfy the slice of the `katzenpost_thinclient.ThinClient`
contract that `katzenqt.network` actually exercises, while never entering
any `thin_client` code at runtime. The point is to test `network.py`
branches against deterministic, controllable replies, so coverage is
driven by the test author rather than by the mixnet's mood.
"""
