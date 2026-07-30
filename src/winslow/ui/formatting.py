def format_status_summary(completed, problematic, total):
    """Render a summary of the completed, problematic and total counts, with a
    color for each count."""
    completed_str = f"[green]{completed}[/green]" if completed else str(completed)
    problematic_str = f"[red]{problematic}[/red]" if problematic else str(problematic)
    return f"{completed_str} / {problematic_str} / {total}"


def format_elapsed(seconds):
    """Render an elapsed duration as a short string, for example 12s, 3m 05s or
    1h 04m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
