"""
Palindrome Checker

Checks whether a given string is a palindrome, ignoring non-alphanumeric characters and case.

Functions:
    is_palindrome(s)

Returns:
    bool: True if the input string is a palindrome, False otherwise.
"""

def is_palindrome(s):
    """
    Checks whether a given string is a palindrome, ignoring non-alphanumeric characters and case.

    Parameters:
        s (str): The input string to check.

    Returns:
        bool: True if the input string is a palindrome, False otherwise.
    """

    # Remove non-alphanumeric characters and convert to lowercase
    s = ''.join(c for c in s if c.isalnum()).lower()

    # Check if the cleaned string is equal to its reverse
    return s == s[::-1]


import unittest

class TestPalindromeChecker(unittest.TestCase):
    def test_palindrome(self):
        self.assertTrue(is_palindrome("A man, a plan, a canal: Panama"))

    def test_not_palindrome(self):
        self.assertFalse(is_palindrome("hello"))

    def test_empty_string(self):
        self.assertTrue(is_palindrome(""))

    def test_single_character(self):
        self.assertTrue(is_palindrome("a"))

    def test_case_insensitivity(self):
        self.assertTrue(is_palindrome("RaDa"))
        self.assertFalse(is_palindrome("rAdA"))

if __name__ == "__main__":
    unittest.main()