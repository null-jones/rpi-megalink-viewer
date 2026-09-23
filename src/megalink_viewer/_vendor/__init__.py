"""Third-party code carried in the package rather than depended on.

The package has no runtime dependencies, and a Pi Zero is a poor place to find
out that a wheel needs compiling. Each file here is an unmodified copy of its
upstream, so it can be replaced wholesale rather than merged into.

qrcodegen.py
    QR Code generator library, Copyright (c) Project Nayuki, MIT License.
    https://github.com/nayuki/QR-Code-generator/blob/777682a64202fdb837b50e351b25b7ddb27852c4/python/qrcodegen.py
    Used to put the configuration page's address on the screen as a code a
    phone can scan.
"""
