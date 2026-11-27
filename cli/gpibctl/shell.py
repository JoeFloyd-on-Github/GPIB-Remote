"""Interactive shell: every gpibctl command on one connection, so locks last between commands."""

import cmd
import shlex

from . import commands


class Shell(cmd.Cmd):
    intro = "GPIB-Remote shell. Commands as in gpibctl (e.g. `ls`, `lock bench-psu`, `set bench-psu output on`).\n" \
            "`help` lists them, `help set` explains one, `quit` leaves (and releases your locks)."

    def __init__(self, ctx):
        super().__init__(stdin=None, stdout=ctx.out)
        self.ctx = ctx
        ctx.interactive = True
        self.parser = commands.build_parser(prog="", with_connection=False)
        self.prompt = "gpib> "
        self.use_rawinput = ctx.inp is input

    def preloop(self):
        info = self.ctx.req("server.info")
        self.ctx.say("connected: server %s, %d board(s)" % (info["version"], len(info["boards"])))

    def precmd(self, line):
        self.ctx.notices()
        return line

    def emptyline(self):
        pass

    def default(self, line):
        if line.strip() in ("quit", "exit", "EOF", "q"):
            return True
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            self.ctx.say("error: %s" % exc)
            return False
        if argv and argv[0] in ("shell", "config"):
            self.ctx.say("not available inside the shell")
            return False
        try:
            args = self.parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code not in (0, None):
                self.ctx.say(exc.code if isinstance(exc.code, str) else "invalid command; try `help`")
            return False
        try:
            commands.run(self.ctx, args)
        except SystemExit as exc:
            if exc.code not in (0, None):
                self.ctx.say(exc.code)
        except KeyboardInterrupt:
            self.ctx.say("interrupted")
        return False

    def do_help(self, arg):
        try:
            self.parser.parse_args((arg.split() if arg else []) + ["-h"])
        except SystemExit:
            pass

    def cmdloop(self, intro=None):
        if self.ctx.inp is not input:  # scripted input (tests): feed lines from ctx.inp
            self.preloop()
            while True:
                try:
                    line = self.ctx.inp(self.prompt)
                except EOFError:
                    break
                if self.onecmd(self.precmd(line)):
                    break
            return
        while True:
            try:
                return super().cmdloop(intro)
            except KeyboardInterrupt:
                self.ctx.say("^C (type quit to leave)")
                intro = ""
