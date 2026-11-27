"""
Standalone Playwright login helper for ok-ru-friends-pathfinder.
Delegates to the unified OKBrowserSession module so paths and .env configuration stay DRY.
"""

from ok_traverser.auth import OKBrowserSession

if __name__ == "__main__":
    OKBrowserSession.interactive_login()
