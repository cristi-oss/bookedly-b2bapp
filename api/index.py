"""
Vercel serverless entry point for Bookedly CRM.
Wraps the Flask app as a WSGI handler.
"""
import os
import sys

# Add parent directory to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crm import app, init_crm

# Initialize DB tables on cold start
init_crm()

# Vercel expects an `app` WSGI callable
