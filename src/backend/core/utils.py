"""Root utils for the core application."""

import json

from configurations import values


class JSONValue(values.Value):
    """
    A custom value class based on django-configurations Value class that
    allows to load a JSON string and use it as a value.
    """

    def to_python(self, value):
        """
        Return the python representation of the JSON string.

        Returns None for empty strings, allowing the default value to be used.
        """
        if not value or not value.strip():
            return None
        return json.loads(value)
