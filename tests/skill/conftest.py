"""Make lambda/ importable the way Alexa-hosted Lambda runs it."""

import os
import sys

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
