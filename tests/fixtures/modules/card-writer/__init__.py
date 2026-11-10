"""A test fixture: a module whose tick writes a card's program file the way a card module does, through kernel/world.py's
door, so the harness's own guard tests need no private module."""
from athena_rex.kernel import world

SUMMARY = "writes a fake card, for the harness's own tests"


def write(text="off\n") -> bool:
    root = world.card_root()
    card = root / "ExampleCard" if root else None
    if card is None or not card.is_dir() or not world.may_write_card(card) or not world.may_write_card(card / "PROGRAM.TXT"):
        return False
    with open(card / "PROGRAM.TXT", "w") as f:
        f.write(text)
    return True


def tick(ctx, now):
    write()
    return []
