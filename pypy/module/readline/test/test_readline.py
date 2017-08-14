# -*- coding: utf-8 -*-

class AppTestReadline:
    spaceconfig = dict(usemodules={
        'unicodedata', 'termios', 'select', 'signal', 'fcntl',
        '_minimal_curses', 'faulthandler', '_socket', 'binascii',
        '_posixsubprocess',
    })

    def test_nonascii_history(self):
        import os, readline
        TESTFN = "{}_{}_tmp".format("@test", os.getpid())

        is_editline = readline.__doc__ and "libedit" in readline.__doc__

        readline.clear_history()
        try:
            readline.add_history("entrée 1")
        except UnicodeEncodeError as err:
            skip("Locale cannot encode test data: " + format(err))
        readline.add_history("entrée 2")
        readline.replace_history_item(1, "entrée 22")
        readline.write_history_file(TESTFN)
        readline.clear_history()
        readline.read_history_file(TESTFN)
        if is_editline:
            # An add_history() call seems to be required for get_history_
            # item() to register items from the file
            readline.add_history("dummy")
        assert readline.get_history_item(1) ==  "entrée 1"
        assert readline.get_history_item(2) == "entrée 22"