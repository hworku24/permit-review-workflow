"""Server rendered screens for department staff.

Templates on the same FastAPI app that serves the API, so there is one process to run and
one thing to deploy. The screens read the same views the JSON endpoints read and call the
same engine, so a screen cannot show a number the API disagrees with.
"""
