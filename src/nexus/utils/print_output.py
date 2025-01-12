from functools import wraps

from rich import print


def print_output(func):
    """Decorator to print the output of a function."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)  # Call the original function
        print(result)  # Print the output
        return result  # Return the result to preserve original functionality

    return wrapper
