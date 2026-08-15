"""Позволяет тестам в tests/ импортировать sar_common.py, sar_video_review.py
и т.д. напрямую из корня проекта, независимо от того, откуда запущен pytest."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
