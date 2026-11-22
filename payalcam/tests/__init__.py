"""Tests run against defaults only: no camlive.toml, synthetic source, fake PTZ, an unused port."""
import logging, os

logging.getLogger("camlive").addHandler(logging.NullHandler())  # keep test output quiet

os.environ["CAMLIVE_CONFIG"] = os.path.join(os.path.dirname(__file__), "no-such-camlive.toml")
os.environ["CAMLIVE_SOURCE"] = "test"
os.environ["CAMLIVE_PTZ"] = "fake"
os.environ["CAMLIVE_PORT"] = "8799"
