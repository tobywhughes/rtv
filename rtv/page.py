import curses
import six
import sys

import praw.errors

from .helpers import clean
from .curses_helpers import Color, show_notification, show_help, text_input
from .docs import AGENT

__all__ = ['Navigator']


class Navigator(object):
    """
    Handles math behind cursor movement and screen paging.
    """

    def __init__(
            self,
            valid_page_cb,
            page_index=0,
            cursor_index=0,
            inverted=False):

        self.page_index = page_index
        self.cursor_index = cursor_index
        self.inverted = inverted
        self._page_cb = valid_page_cb
        self._header_window = None
        self._content_window = None

    @property
    def step(self):
        return 1 if not self.inverted else -1

    @property
    def position(self):
        return (self.page_index, self.cursor_index, self.inverted)

    @property
    def absolute_index(self):
        return self.page_index + (self.step * self.cursor_index)

    def move(self, direction, n_windows):
        "Move the cursor down (positive direction) or up (negative direction)"

        valid, redraw = True, False

        forward = ((direction * self.step) > 0)

        if forward:
            if self.page_index < 0:
                if self._is_valid(0):
                    # Special case - advance the page index if less than zero
                    self.page_index = 0
                    self.cursor_index = 0
                    redraw = True
                else:
                    valid = False
            else:
                self.cursor_index += 1
                if not self._is_valid(self.absolute_index):
                    # Move would take us out of bounds
                    self.cursor_index -= 1
                    valid = False
                elif self.cursor_index >= (n_windows - 1):
                    # Flip the orientation and reset the cursor
                    self.flip(self.cursor_index)
                    self.cursor_index = 0
                    redraw = True
        else:
            if self.cursor_index > 0:
                self.cursor_index -= 1
            else:
                self.page_index -= self.step
                if self._is_valid(self.absolute_index):
                    # We have reached the beginning of the page - move the
                    # index
                    redraw = True
                else:
                    self.page_index += self.step
                    valid = False  # Revert

        return valid, redraw

    def flip(self, n_windows):
        "Flip the orientation of the page"

        self.page_index += (self.step * n_windows)
        self.cursor_index = n_windows
        self.inverted = not self.inverted

    def _is_valid(self, page_index):
        "Check if a page index will cause entries to fall outside valid range"

        try:
            self._page_cb(page_index)
        except IndexError:
            return False
        else:
            return True


class BaseController(object):
    """
    Event handler for triggering functions with curses keypresses.

    Register a keystroke to a class method using the @egister decorator.
    #>>> @Controller.register('a', 'A')
    #>>> def func(self, *args)

    Register a default behavior by using `None`.
    #>>> @Controller.register(None)
    #>>> def default_func(self, *args)

    Bind the controller to a class instance and trigger a key. Additional
    arguments will be passed to the function.
    #>>> controller = Controller(self)
    #>>> controller.trigger('a', *args)
    """

    character_map = {None: (lambda *args, **kwargs: None)}

    def __init__(self, instance):
        self.instance = instance

    def trigger(self, char, *args, **kwargs):

        if isinstance(char, six.string_types) and len(char) == 1:
            char = ord(char)

        func = self.character_map.get(char)
        if func is None:
            func = BaseController.character_map.get(char)
        if func is None:
            func = self.character_map.get(None)
        if func is None:
            func = BaseController.character_map.get(None)
        return func(self.instance, *args, **kwargs)

    @classmethod
    def register(cls, *chars):
        def wrap(f):
            for char in chars:
                if isinstance(char, six.string_types) and len(char) == 1:
                    cls.character_map[ord(char)] = f
                else:
                    cls.character_map[char] = f
            return f
        return wrap


class BasePage(object):
    """
    Base terminal viewer incorperates a cursor to navigate content
    """

    MIN_HEIGHT = 10
    MIN_WIDTH = 20

    def __init__(self, stdscr, reddit, content, **kwargs):

        self.stdscr = stdscr
        self.reddit = reddit
        self.content = content
        self.nav = Navigator(self.content.get, **kwargs)

        self._header_window = None
        self._content_window = None
        self._subwindows = None

    @BaseController.register('q')
    def exit(self):
        sys.exit()

    @BaseController.register('?')
    def help(self):
        show_help(self._content_window)

    @BaseController.register(curses.KEY_UP, 'k')
    def move_cursor_up(self):
        self._move_cursor(-1)
        self.clear_input_queue()

    @BaseController.register(curses.KEY_DOWN, 'j')
    def move_cursor_down(self):
        self._move_cursor(1)
        self.clear_input_queue()

    def clear_input_queue(self):
        "Clear excessive input caused by the scroll wheel or holding down a key"

        self.stdscr.nodelay(1)
        while self.stdscr.getch() != -1:
            continue
        self.stdscr.nodelay(0)

    @BaseController.register('a')
    def upvote(self):
        data = self.content.get(self.nav.absolute_index)
        try:
            if 'likes' not in data:
                pass
            elif data['likes']:
                data['object'].clear_vote()
                data['likes'] = None
            else:
                data['object'].upvote()
                data['likes'] = True
        except praw.errors.LoginOrScopeRequired:
            show_notification(self.stdscr, ['Not logged in'])

    @BaseController.register('z')
    def downvote(self):
        data = self.content.get(self.nav.absolute_index)
        try:
            if 'likes' not in data:
                pass
            if data['likes'] is False:
                data['object'].clear_vote()
                data['likes'] = None
            else:
                data['object'].downvote()
                data['likes'] = False
        except praw.errors.LoginOrScopeRequired:
            show_notification(self.stdscr, ['Not logged in'])

    @BaseController.register('u')
    def login(self):
        """
        Prompt to log into the user's account, or log out of the current
        account.
        """

        if self.reddit.is_logged_in():
            self.logout()
            return

        username = self.prompt_input('Enter username:')
        password = self.prompt_input('Enter password:', hide=True)
        if not username or not password:
            curses.flash()
            return

        try:
            with self.loader():
                self.reddit.login(username, password)
        except praw.errors.InvalidUserPass:
            show_notification(self.stdscr, ['Invalid user/pass'])
        else:
            show_notification(self.stdscr, ['Welcome {}'.format(username)])

    def logout(self):
        "Prompt to log out of the user's account."

        ch = self.prompt_input("Log out? (y/n):")
        if ch == 'y':
            self.reddit.clear_authentication()
            show_notification(self.stdscr, ['Logged out'])
        elif ch != 'n':
            curses.flash()

    def prompt_input(self, prompt, hide=False):
        "Prompt the user for input"

        attr = curses.A_BOLD | Color.CYAN
        n_rows, n_cols = self.stdscr.getmaxyx()

        if hide:
            prompt += ' ' * (n_cols - len(prompt) - 1)
            self.stdscr.addstr(n_rows-1, 0, prompt, attr)
            out = self.stdscr.getstr(n_rows-1, 1)
        else:
            self.stdscr.addstr(n_rows - 1, 0, prompt, attr)
            self.stdscr.refresh()
            window = self.stdscr.derwin(1, n_cols - len(prompt),
                                        n_rows - 1, len(prompt))
            window.attrset(attr)
            out = text_input(window)

        return out

    def draw(self):

        n_rows, n_cols = self.stdscr.getmaxyx()
        if n_rows < self.MIN_HEIGHT or n_cols < self.MIN_WIDTH:
            return

        # Note: 2 argument form of derwin breaks PDcurses on Windows 7!
        self._header_window = self.stdscr.derwin(1, n_cols, 0, 0)
        self._content_window = self.stdscr.derwin(n_rows - 1, n_cols, 1, 0)

        self.stdscr.erase()
        self._draw_header()
        self._draw_content()
        self._add_cursor()

    @staticmethod
    def draw_item(window, data, inverted):
        raise NotImplementedError

    def _draw_header(self):

        n_rows, n_cols = self._header_window.getmaxyx()

        self._header_window.erase()
        attr = curses.A_REVERSE | curses.A_BOLD | Color.CYAN
        self._header_window.bkgd(' ', attr)

        sub_name = self.content.name.replace('/r/front', 'Front Page ')
        self._header_window.addnstr(0, 0, clean(sub_name), n_cols - 1)

        if self.reddit.user is not None:
            username = self.reddit.user.name
            s_col = (n_cols - len(username) - 1)
            # Only print the username if it fits in the empty space on the
            # right
            if (s_col - 1) >= len(sub_name):
                n = (n_cols - s_col - 1)
                self._header_window.addnstr(0, s_col, clean(username), n)

        self._header_window.refresh()

    def _draw_content(self):
        """
        Loop through submissions and fill up the content page.
        """

        n_rows, n_cols = self._content_window.getmaxyx()
        self._content_window.erase()
        self._subwindows = []

        page_index, cursor_index, inverted = self.nav.position
        step = self.nav.step

        # If not inverted, align the first submission with the top and draw
        # downwards. If inverted, align the first submission with the bottom
        # and draw upwards.
        current_row = (n_rows - 1) if inverted else 0
        available_rows = (n_rows - 1) if inverted else n_rows
        for data in self.content.iterate(page_index, step, n_cols - 2):
            window_rows = min(available_rows, data['n_rows'])
            window_cols = n_cols - data['offset']
            start = current_row - window_rows if inverted else current_row
            subwindow = self._content_window.derwin(
                window_rows, window_cols, start, data['offset'])
            attr = self.draw_item(subwindow, data, inverted)
            self._subwindows.append((subwindow, attr))
            available_rows -= (window_rows + 1)  # Add one for the blank line
            current_row += step * (window_rows + 1)
            if available_rows <= 0:
                break
        else:
            # If the page is not full we need to make sure that it is NOT
            # inverted. Unfortunately, this currently means drawing the whole
            # page over again. Could not think of a better way to pre-determine
            # if the content will fill up the page, given that it is dependent
            # on the size of the terminal.
            if self.nav.inverted:
                self.nav.flip((len(self._subwindows) - 1))
                self._draw_content()

        self._content_window.refresh()

    def _add_cursor(self):
        self._edit_cursor(curses.A_REVERSE)

    def _remove_cursor(self):
        self._edit_cursor(curses.A_NORMAL)

    def _move_cursor(self, direction):

        self._remove_cursor()

        valid, redraw = self.nav.move(direction, len(self._subwindows))
        if not valid:
            curses.flash()

        # Note: ACS_VLINE doesn't like changing the attribute, so always redraw.
        # if redraw: self._draw_content()
        self._draw_content()
        self._add_cursor()

    def _edit_cursor(self, attribute=None):

        # Don't allow the cursor to go below page index 0
        if self.nav.absolute_index < 0:
            return

        window, attr = self._subwindows[self.nav.cursor_index]
        if attr is not None:
            attribute |= attr

        n_rows, _ = window.getmaxyx()
        for row in range(n_rows):
            window.chgat(row, 0, 1, attribute)

        window.refresh()