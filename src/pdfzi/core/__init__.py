"""Pure PDF logic. Must never import Textual or Typer, or touch the network."""

import warnings

from PIL import Image

# Pillow warns (on stderr, straight over the terminal UI) about pictures between 89 and 178
# megapixels. pdfzi sets its own pixel limits and reports them in words, so the warning
# only gets in the way. Above 178 megapixels Pillow still raises an error, which is handled.
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
