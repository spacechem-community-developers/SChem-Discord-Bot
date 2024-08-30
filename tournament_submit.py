#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path

import discord
from discord.ext import commands
import schem

from metric import eval_metric, get_metric_and_terms, has_runtime_metrics, cycle_handler
from tournament_base import BaseTournament, is_tournament_host


class TournamentSubmit(BaseTournament):
    """Submission-related tournament commands and utils."""

    is_host = commands.check(is_tournament_host)

    DEFAULT_MAX_CYCLES = 1_000_000

    @staticmethod
    async def parse_solution_attachment(attachment: discord.Attachment, is_host=False):
        """Given a discord Attachment expected to be a solution file, return a list of its solutions.
        If is_host is False, assert that only one solution is included.
        """
        soln_bytes = await attachment.read()
        try:
            soln_str = soln_bytes.decode("utf-8").replace('\r\n', '\n')
        except UnicodeDecodeError as e:
            raise Exception("Attachment must be a plaintext file (containing a Community Edition export).") from e

        soln_strs = list(schem.Solution.split_solutions(soln_str))
        assert len(soln_strs) != 0, "Attachment does not contain SpaceChem solution string(s)"
        assert len(soln_strs) == 1 or is_host, "Expected only one solution in the attached file."

        return soln_strs

    @staticmethod
    def verify_round_submission_time(submission_msg: discord.Message, tournament_metadata: dict, puzzle_name: str,
                                     ignore_end=False):
        """Raise an appropriate error if the given puzzle does not exist or the given message was not sent/edited
        during its submission time.
        """
        # Since otherwise future puzzle names could in theory be searched for, make sure we return the same message
        # whether a puzzle does not exist or is not yet open for submissions
        unknown_level_exc = ValueError(f"No active tournament level `{puzzle_name}`; ensure the first line of"
                                       + " your solution export has the correct level name.")
        if puzzle_name not in tournament_metadata['rounds']:
            raise unknown_level_exc
        round_metadata = tournament_metadata['rounds'][puzzle_name]

        if submission_msg.edited_at is not None:
            # Cover any possible late-submission exploits if we ever allow bot to respond to edits
            msg_time = submission_msg.edited_at.replace(tzinfo=timezone.utc)
        else:
            msg_time = submission_msg.created_at.replace(tzinfo=timezone.utc)

        if msg_time < datetime.fromisoformat(round_metadata['start']):
            raise unknown_level_exc
        elif msg_time > datetime.fromisoformat(round_metadata['end']) and not ignore_end:
            raise Exception(f"Submissions for `{puzzle_name}` have closed.")

    @staticmethod
    def add_or_check_player(round_dir: Path, user: discord.User, nickname: str):
        """Given a discord user and nickname, register this participant or verify they match the originally-registered
        pairing. Return their team name if any or else None.
        """
        with open(round_dir.parent / 'participants.json', 'r', encoding='utf-8') as f:
            participants = json.load(f)
        with open(round_dir / 'teams.json', encoding='utf-8') as f:
            teams = json.load(f)

        discord_tag = str(user)

        # If the player is part of a team and submitted under their team name, just return their team name
        if nickname in teams and discord_tag in teams[nickname]:
            return nickname

        # If they aren't submitting under a team name, check if the nickname has already been set or must be set now
        if discord_tag in participants and 'name' in participants[discord_tag]:
            if nickname != participants[discord_tag]['name']:
                # TODO: Could allow name changes but it would be a lot of work and potentially confusing for the
                #       other participants, probably should only do this case-by-case and manually
                raise ValueError(f"Given author name `{nickname}` doesn't match your prior submissions':"
                                 + f" `{participants[discord_tag]['name']}`; please talk to the"
                                 + " tournament host if you would like a name change.")

            # If the above teams check didn't match and the nickname is registered, bad shit is happening
            assert nickname not in teams, "It looks like a team with your name exists, please yell at Zig"
        else:
            # Set nickname for the first time
            # TODO: Add teams listing to tournament metadata for checking for conflicts with other rounds' teams
            if nickname in teams or any('name' in d and nickname == d['name'] for d in participants.values()):
                raise PermissionError(f"Solution author name `{nickname}` is already in use by another player or team,"
                                      + " please choose another (or login to the correct discord account).")

            if discord_tag not in participants:
                participants[discord_tag] = {'id': user.id}

            # Separate from the above since if added by a team, a player may already exist but not have had a name
            participants[discord_tag]['name'] = nickname

            with open(round_dir.parent / 'participants.json', 'w', encoding='utf-8') as f:
                json.dump(participants, f, ensure_ascii=False, indent=4)

        # After adding nickname if possible, check if this user is in any team and return their team name if so
        for team_name, tags in teams.items():
            if discord_tag in tags:
                return team_name

        return None

    @commands.Cog.listener('on_message')
    async def tournament_submit_shortcut(self, msg):
        """Treat any non-command DM message as a call to tournament-submit."""
        if not msg.content.startswith(self.bot.command_prefix) and msg.guild is None:
            msg.content = f"{self.bot.command_prefix}tournament-submit {msg.content}"
            await self.bot.process_commands(msg)
        # else do nothing as this does not replace the regular bot listener which will work on prefixed commands

    # TODO: Give the bot permission to delete !tournament-submit messages from public channels
    #       since someone will inevitably forget to use DMs
    # TODO: Kick host submit backdoor into own command and merge code with submit_fun
    @commands.command(name='tournament-submit', aliases=['ts'])
    @commands.dm_only()
    async def tournament_submit(self, ctx, *, comment=""):
        """Submit the given solution text (either raw or an attached file) to the tournament.

        If no attachment is included, the raw text is instead checked for being a solution export,
        to be submitted without comment.

        [Optional] comment: Comment to go with the solution. When the puzzle results
                            are announced, your submission history will be publicly
                            published along with any of these comments.
        [Attachment] *: A text file containing a single Community Edition-exported
                        solution string. To create this, open the solution then click
                        Export from the CE save menu (hamburger button below Undo).
        """
        tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata()
        is_host = is_tournament_host(ctx)

        assert len(ctx.message.attachments) <= 1, "Expected at most one attachment!"

        if len(ctx.message.attachments) == 1:
            soln_strs = await self.parse_solution_attachment(ctx.message.attachments[0], is_host=is_host)
        else:
            # Check if the message text is instead a raw solution string
            try:
                soln_strs = list(schem.Solution.split_solutions(comment))
            except ValueError:
                raise Exception("Message does not contain a SpaceChem solution")
            comment = ""  # Make sure we don't store the comment in history

            assert len(soln_strs) != 0, "Message does not contain a SpaceChem solution"
            assert len(soln_strs) <= 1 or is_host, "Expected only one solution export."

        reaction = '✅'
        for soln_str in soln_strs:
            msg = None
            try:
                level_name, author, expected_score, soln_name = schem.Solution.parse_metadata(soln_str)

                # Check the round exists and the message is within its submission period
                self.verify_round_submission_time(ctx.message, tournament_metadata, level_name)

                round_metadata = tournament_metadata['rounds'][level_name]
                round_dir = tournament_dir / round_metadata['dir']

                with self.puzzle_submission_locks[level_name]:
                    level = self.get_level(round_dir)

                    # Verify the solution
                    # TODO: Provide seconds or minutes ETA based on estimate of 2,000,000 cycles / min (/ reactor?)
                    soln_descr = schem.Solution.describe(level_name, author, expected_score, soln_name)
                    msg = await ctx.send(f"Running {soln_descr}, this should take < 30s barring an absurd cycle count...")

                    solution = schem.Solution(soln_str, level=level)

                    # Skip participant name checks for the TO backdoor
                    if not is_tournament_host(ctx):
                        team_name = self.add_or_check_player(round_dir, ctx.message.author, author)

                        # Change the author name if the submitter is part of a team
                        if team_name is not None:
                            author = team_name
                            solution.author = author

                    # Prefix the solution name with "[author] " for readability on import
                    solution.name = f"[{author}]" if soln_name is None else f"[{author}] {soln_name}"

                    # If the metric contains any special vars that require runtime-measurement, pass a corresponding
                    # handler to Solution.validate.
                    metric = round_metadata['metric']

                    # Call the SChem validator in a thread so the bot isn't blocked
                    # TODO: ProcessPoolExecutor might be more appropriate instead of the default (thread pool), but not
                    #  sure if the overhead for many small submissions is going to add up more than with threads and/or
                    #  if limitations on number of processes is the bigger factor
                    max_cycles = round_metadata['max_cycles'] if 'max_cycles' in round_metadata else self.DEFAULT_MAX_CYCLES
                    hash_states = 1000  # Unfortunate side effect of run_in_executor, can't use keyword args...
                    loop = asyncio.get_event_loop()
                    # Allow submissions to omit the expected score, in which case it will be calculated for them.
                    # Required for computation puzzles. For now, Solution.validate will still be used in the case of an
                    # expected score being provided so I can maintain the possibility of catching schem bugs
                    # TODO: if metric uses 'outputs' as a var, we should instead catch any run errors (or just
                    #       PauseException, to taste) and pass the post-run solution object to eval_metric regardless
                    if solution.expected_score is not None:
                        await loop.run_in_executor(None, solution.validate, max_cycles, 0, cycle_handler(metric))  # Default thread pool executor
                    else:
                        # Calculate the score and update the solution object with it (so eval_metric works below)
                        solution.expected_score = await loop.run_in_executor(None, solution.run, max_cycles, 0, cycle_handler(metric))

                    # Calculate the solution's metric score and report the values of any non-standard terms.
                    soln_metric_score, metric_terms = get_metric_and_terms(solution, metric)

                    special_metrics_descr = ""
                    for metric_term, value in metric_terms.items():
                        if metric_term not in ('cycles', 'symbols', 'reactors'):
                            special_metrics_descr += f"\n`{metric_term}`: `{value}`"

                    await msg.edit(content=f"Successfully validated {soln_descr}, metric score: `{round(soln_metric_score, 3)}`"
                                           + special_metrics_descr
                                           + f"\n{self.puzzle_deadline_str(round_metadata)}.")

                    # Update solutions.txt
                    # To ensure async-safe file-writing, we may need to read the file(s) twice: the first time to ask
                    # the user if they're ok with regressing their metric, and the second to make sure we pick up any
                    # updates to the file that happened while we were waiting for the user to confirm the regression.
                    # If there's no regression the second read can be avoided.
                    # This leaves the small edge case that the second read will blindly assume the player is fine with
                    # the regression even if e.g. a team member just made their regression even bigger... but that
                    # should be acceptable given they were ok with any regression at all
                    for i in range(2):
                        # If the puzzle has any runtime metrics, we store the runtime metric vars in an extra file so we
                        # don't have to re-run the solution later.
                        if has_runtime_metrics(metric):
                            with open(round_dir / 'runtime_metrics.json', 'r', encoding='utf-8') as f:
                                runtime_metrics = json.load(f)

                        with open(round_dir / 'solutions.txt', 'r', encoding='utf-8') as f:
                            solns_str = f.read()

                        new_soln_strs = []
                        for cur_soln_str in schem.Solution.split_solutions(solns_str):
                            _, cur_author, _, _ = schem.Solution.parse_metadata(cur_soln_str)
                            if cur_author == author:
                                # On the second read we are safe to assume the user is okay with the regression
                                if i == 1:
                                    continue

                                # Warn the user if their submission regresses the metric score
                                # (we will still allow the submission in case they wanted to submit something
                                # sub-optimal for style/meme/whatever reasons)
                                # Note: This re-does the work of calculating the old metric but is simpler and allows
                                #       the TO to modify the metric after the puzzle opens if necessary
                                old_solution = schem.Solution(cur_soln_str, level=level)
                                # Stuff old runtime metrics in as needed
                                if has_runtime_metrics(metric):
                                    assert author in runtime_metrics, "You're in solutions.txt but not runtime_metrics.json, yell at Zig"
                                    old_solution.custom_data = runtime_metrics[author]
                                old_metric_score = eval_metric(old_solution, metric)
                                if soln_metric_score > old_metric_score:
                                    await ctx.message.add_reaction('⚠')

                                    confirm_msg = await ctx.send(
                                        "Warning: This solution regresses your last submission's metric score,"
                                        f"previously: {round(old_metric_score, 3)}"
                                        "\nAre you sure you wish to continue?"
                                        " React with ✅ within 30 seconds to proceed, ❌ to cancel all changes.")
                                    if not await self.wait_for_confirmation(ctx, confirm_msg):
                                        await ctx.message.add_reaction('❌')
                                        return

                                    break
                            else:
                                new_soln_strs.append(cur_soln_str)
                        else:
                            # If we didn't break in the loop, we did no async operations and can skip the re-read
                            new_soln_strs.append(solution.export_str())
                            break

                    with open(round_dir / 'solutions.txt', 'w', encoding='utf-8') as f:
                        # Make sure not to write windows newlines or python will double the carriage returns
                        f.write('\n'.join(new_soln_strs))

                    if has_runtime_metrics(metric):
                        # Delete any temporary vars (we don't need them and they might not serialize)
                        for k in list(solution.custom_data.keys()):
                            if k.startswith('_'):
                                del solution.custom_data[k]

                        runtime_metrics[author] = solution.custom_data
                        with open(round_dir / 'runtime_metrics.json', 'w', encoding='utf-8') as f:
                            json.dump(runtime_metrics, f, ensure_ascii=False, indent=4)

                    # Write the submission time, score, metric, and any comment to this author's submission history
                    with open(round_dir / 'submissions_history.json', 'r', encoding='utf-8') as f:
                        submissions_history = json.load(f)

                    if author not in submissions_history:
                        submissions_history[author] = []
                        # Sort by author name, case-insensitively
                        submissions_history = {k: submissions_history[k] for k in sorted(submissions_history,
                                                                                         key=lambda s: s.lower())}

                    submit_time = (ctx.message.created_at if ctx.message.edited_at is None
                                   else ctx.message.edited_at).replace(tzinfo=timezone.utc)

                    submissions_history[author].append((submit_time.isoformat(),
                                                        str(solution.expected_score),
                                                        soln_metric_score,
                                                        solution.name if solution.name else None,
                                                        comment if comment else None))

                    with open(round_dir / 'submissions_history.json', 'w', encoding='utf-8') as f:
                        json.dump(submissions_history, f, ensure_ascii=False, indent=4)
            except Exception as e:
                reaction = '❌'
                print(f"{type(e).__name__}: {e}")
                # Replace the 'Running...' message if it got that far
                if msg is not None:
                    await msg.edit(content=f"```{type(e).__name__}: {e}```")
                else:
                    await ctx.send(f"```{type(e).__name__}: {e}```")

        await ctx.message.add_reaction(reaction)

    # TODO: Limit each player to 5 non-scoring solutions
    # TODO: Give the bot permission to delete !tournament-submit messages from public channels
    #       since someone will inevitably forget to use DMs
    @commands.command(name='tournament-submit-fun', aliases=['tsf', 'tournament-submit-non-scoring', 'tsns'])
    @commands.dm_only()
    async def tournament_submit_fun(self, ctx):
        """Submit the attached *non-scoring* solution file to the tournament.

        Will replace any previous non-scoring solution of the same name.
        There are currently no limits on how many non-scoring solutions you may submit;
        please keep it reasonable (and use tournament-submission-fun-remove as needed).
        """
        tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata()

        assert len(ctx.message.attachments) == 1, "Expected one attached solution file!"
        soln_str = (await self.parse_solution_attachment(ctx.message.attachments[0]))[0]

        level_name, author, expected_score, soln_name = schem.Solution.parse_metadata(soln_str)

        # Check the round exists and the message is after its start date. Ignore the end date for fun submissions
        self.verify_round_submission_time(ctx.message, tournament_metadata, level_name, ignore_end=True)

        round_metadata = tournament_metadata['rounds'][level_name]
        round_dir = tournament_dir / round_metadata['dir']

        # TODO: Check if the tournament host set a higher max submission cycles value, otherwise default to e.g.
        #       10,000,000 and break here if that's violated

        # Since we allow non-scoring submissions to be sent after the deadline, the lock may not exist.
        # We'll still grab it while it does, to avoid conflicts with results publication of solutions_fun.txt
        with (self.puzzle_submission_locks[level_name] if level_name in self.puzzle_submission_locks else nullcontext()):
            level = self.get_level(round_dir)

            # Verify the solution
            # TODO: Provide seconds or minutes ETA based on estimate of 2,000,000 cycles / min (/ reactor?)
            soln_descr = schem.Solution.describe(level_name, author, expected_score, soln_name)
            msg = await ctx.send(f"Running {soln_descr}, this should take < 30s barring an absurd cycle count...")

            solution = schem.Solution(soln_str, level=level)

            # Register or verify this participant's nickname
            team_name = self.add_or_check_player(round_dir, ctx.message.author, author)

            # Change the author name if the submitter is part of a team
            if team_name is not None:
                author = team_name
                solution.author = author

            # Prefix the solution name with "[author] " for readability on import
            solution.name = f"[{author}]" if soln_name is None else f"[{author}] {soln_name}"

            # Call the SChem validator in a thread so the bot isn't blocked
            max_cycles = round_metadata['max_cycles'] if 'max_cycles' in round_metadata else self.DEFAULT_MAX_CYCLES
            loop = asyncio.get_event_loop()
            # Allow submissions to omit the expected score, in which case it will be calculated for them.
            # Required for computation puzzles. For now, Solution.validate will still be used in the case of an
            # expected score being provided so I can maintain the possibility of catching schem bugs
            # TODO: if metric uses 'outputs' as a var, we should instead catch any run errors (or just
            #       PauseException, to taste) and pass the post-run solution object to eval_metric regardless
            if solution.expected_score is not None:
                await loop.run_in_executor(None, solution.validate, max_cycles, 0)  # Default thread pool executor
            else:
                # Calculate the score and update the solution object with it (so eval_metric works below)
                solution.expected_score = await loop.run_in_executor(None, solution.run, max_cycles, 0)

            reply = f"Added non-scoring submission {soln_descr}"

            # Add to solutions_fun.txt, replacing any existing solution by this author if it has the same solution name
            with open(round_dir / 'solutions_fun.txt', 'r', encoding='utf-8') as f:
                solns_str = f.read()

            new_soln_strs = []
            for cur_soln_str in schem.Solution.split_solutions(solns_str):
                _, cur_author, _, cur_soln_name = schem.Solution.parse_metadata(cur_soln_str)
                if cur_author == author and cur_soln_name == solution.name:
                    if solution.name == f"[{author}]":  # The default label only
                        reply += "\nWarning: Solution has no name, and replaces your previous unnamed fun submission." \
                                 + " Consider naming your fun submissions for readability and to submit multiple of them!"
                    else:
                        reply += "\nReplaces previous fun submission of same name."
                else:
                    new_soln_strs.append(cur_soln_str)

            new_soln_strs.append(solution.export_str())

            with open(round_dir / 'solutions_fun.txt', 'w', encoding='utf-8') as f:
                # Make sure not to write windows newlines or python will double the carriage returns
                f.write('\n'.join(new_soln_strs))

        # TODO: Update submissions_history.txt with time, name, score, and blurb

        await ctx.message.add_reaction('✅')
        await msg.edit(content=f"{reply}\n{self.puzzle_deadline_str(round_metadata)}.")

    @commands.command(name='tournament-submissions-list', aliases=['tsl',
                                                                   'tournament-submissions',
                                                                   'tournament-list-submissions', 'tls'])
    @commands.dm_only()
    async def tournament_list_submissions(self, ctx, *, round_or_puzzle_name=None):
        """List your puzzle submissions.

        If round/puzzle not specified, shows only currently-active puzzle submissions.

        round_or_puzzle_name: (Case-insensitive) If provided, show only your submissions
                              to the specified round/puzzle. May be a past puzzle.
                              A string like r10 will also match "Round 10" as a shortcut.
        """
        tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata(is_host=False)
        nickname = self.get_player_name(tournament_dir, ctx.message.author)

        reply = ""

        if round_or_puzzle_name is None:
            puzzle_names = [puzzle_name for puzzle_name, round_metadata in tournament_metadata['rounds'].items()
                            if 'start_post' in round_metadata and 'end_post' not in round_metadata]
        else:
            puzzle_names = [self.get_puzzle_name(tournament_metadata, round_or_puzzle_name,
                                                 is_host=False, missing_ok=False)]

        for puzzle_name in puzzle_names:
            round_metadata = tournament_metadata['rounds'][puzzle_name]
            round_dir = tournament_dir / round_metadata['dir']

            indent = ''
            if round_or_puzzle_name is None:
                reply += f"\n**{round_metadata['round_name']}, {puzzle_name}**:"
                indent = '    '

            # Identify the name their submissions are stored under, accounting for this round's teams
            team_name = self.get_team_name(round_dir, str(ctx.message.author))
            submit_name = team_name if team_name is not None else nickname
            if submit_name is None:
                reply += f"\n{indent}No submissions."
                continue

            # Check for scoring solution
            with open(round_dir / 'solutions.txt', 'r', encoding='utf-8') as f:
                solns_str = f.read()

            runtime_metrics = None
            if has_runtime_metrics(round_metadata['metric']):
                with open(round_dir / 'runtime_metrics.json', 'r', encoding='utf-8') as f:
                    runtime_metrics = json.load(f)[submit_name]

            has_scoring = False
            for soln_str in schem.Solution.split_solutions(solns_str):
                _, cur_author, score, soln_name = schem.Solution.parse_metadata(soln_str)
                if cur_author == submit_name:
                    has_scoring = True
                    level = self.get_level(round_dir)
                    reply += f"\n{indent}Scoring submission: {score}"
                    if soln_name is not None:
                        reply += ' ' + soln_name
                    solution = schem.Solution(soln_str, level=level)
                    solution.custom_data = runtime_metrics
                    reply += f", Metric Score: {eval_metric(solution, round_metadata['metric'])}"
                    break
            if not has_scoring:
                reply += f"\n{indent}No scoring submission."

            # Check for non-scoring solutions
            with open(round_dir / 'solutions_fun.txt', 'r', encoding='utf-8') as f:
                fun_solns_str = f.read()

            fun_soln_lines = []
            for soln_str in schem.Solution.split_solutions(fun_solns_str):
                _, cur_author, score, soln_name = schem.Solution.parse_metadata(soln_str)
                if cur_author == submit_name:
                    line = f"    {score}"
                    if soln_name is not None:
                        line += ' ' + soln_name
                    fun_soln_lines.append(line)
            if fun_soln_lines:
                reply += f"\n{indent}Non-scoring submissions:\n" + '\n'.join(fun_soln_lines)

        if not reply:
            await ctx.send("No active puzzles!")
        else:
            await ctx.send(reply.strip())  # Quick hack to remove newline prefix

    @commands.command(name='tournament-submission-fun-remove', aliases=['tsfr',
                                                                        'tournament-submission-non-scoring-remove',
                                                                        'tournament-remove-fun-submission',
                                                                        'tournament-remove-non-scoring-submission'])
    @commands.dm_only()
    async def tournament_remove_fun_submission(self, ctx, round_or_puzzle_name, *, soln_name=None):
        """Remove a non-scoring submission to the given round/puzzle.

        round_or_puzzle_name: (Case-insensitive) The matching round/puzzle to remove a NS solution from.
                              Must be quoted if contains spaces.
                              A string like r10 will also match "Round 10" as a shortcut.
        soln_name: The name of the NS solution to remove (case-sensitive). If omitted, remove unnamed solution.
        """
        tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata(is_host=False)
        puzzle_name = self.get_puzzle_name(tournament_metadata, round_or_puzzle_name, is_host=False, missing_ok=False)
        round_metadata = tournament_metadata['rounds'][puzzle_name]
        round_dir = tournament_dir / round_metadata['dir']

        nickname = self.get_player_name(tournament_dir, ctx.message.author)
        team_name = self.get_team_name(round_dir, str(ctx.message.author))
        submit_name = team_name if team_name is not None else nickname
        if submit_name is None:
            raise Exception("You have no current submissions to this round.")

        # If the user passes no name or an empty string, interpret as an unnamed solution (which we store as "[author]")
        if not soln_name:
            soln_name = f"[{submit_name}]"
        elif not soln_name.startswith(f"[{submit_name}]"):  # No space so the error is legible if they messed up prefixing it themselves
            # Auto-prepend the author name we prefix onto submissions, if the user did not
            soln_name = f"[{submit_name}] " + soln_name

        # Prevent removal from a round whose results have already been published
        if 'end_post' in round_metadata:
            raise ValueError("Cannot remove submission from already-closed round!")

        with self.puzzle_submission_locks[puzzle_name]:
            with open(round_dir / 'solutions_fun.txt', 'r', encoding='utf-8') as f:
                solns_str = f.read()

            new_soln_strs = []
            reply = None
            for cur_soln_str in schem.Solution.split_solutions(solns_str):
                _, cur_author, score, cur_soln_name = schem.Solution.parse_metadata(cur_soln_str)
                if cur_author == submit_name and cur_soln_name == soln_name:
                    if reply is not None:
                        print(f"Internal Error: multiple non-scoring solutions to {puzzle_name} by {submit_name} have same name {soln_name}")

                    if soln_name is None:
                        reply = f"Removed unnamed non-scoring solution: {score}"
                    else:
                        reply = f"Removed non-scoring solution: {score}, {soln_name}"
                else:
                    new_soln_strs.append(cur_soln_str)

            if reply is None:
                if soln_name is None:
                    raise ValueError("No unnamed non-scoring solution found; add solution name argument"
                                     + f" or double-check `!tournament-list-submissions {puzzle_name}`")

                raise ValueError(f"No non-scoring solution named `{soln_name}` found;"
                                 + f" double-check `!tournament-list-submissions {puzzle_name}`")

            with open(round_dir / 'solutions_fun.txt', 'w', encoding='utf-8') as f:
                # Make sure not to write windows newlines or python will double the carriage returns
                f.write('\n'.join(new_soln_strs))

        await ctx.send(reply)

    # TODO: DDOS-mitigating measures such as:
    #       - maximum expected cycle count (set to e.g. 1 million unless specified otherwise for a puzzle) above which
    #         solutions are rejected and requested to be sent directly to the tournament host
    #       - limit user submissions to like 2 per minute

    @commands.command(name='tournament-submission-delete', aliases=['tsd', 'tournament-delete-submission', 'tds'])
    @is_host
    async def delete_submission(self, ctx, round_or_puzzle_name, submit_name, submit_time, *, reason=None):
        """Delete a scoring submission from the active solutions and submit history.

        If the target is the player's active scoring submission, the `reason` arg must
        be included, and will be included in a DM to the player informing them of the
        deleted submission.
        Only intended for use on submissions that violate a puzzle rule.
        Deleting from history too is done to ensure that results graphs are not
        skewed by the rule-violating score.

        round_or_puzzle_name: (Case-insensitive) The round/puzzle name of the submission.
                              A string like r10 will also match "Round 10" as a shortcut.
        submit_name: The team/player nickname the solution appears under in !history.
        submit_time: The ISO format datetime exactly matching that displayed in the
                     submission's !history entry.
        reason: Required if the submission is the player's active submission.
                A brief comment explaining the reason for the removal
                E.g. "Violates 'must run forever' rule.".
        """
        tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata(is_host=True)
        puzzle_name = self.get_puzzle_name(tournament_metadata, round_or_puzzle_name, is_host=True, missing_ok=False)
        round_metadata = tournament_metadata['rounds'][puzzle_name]
        round_dir = tournament_dir / round_metadata['dir']

        if 'start_post' not in round_metadata or 'end_post' in round_metadata:
            raise Exception(f"Cannot modify submissions on non-open round {round_metadata['round_name']}.")

        affected_discord_ids = set()
        with self.puzzle_submission_locks[puzzle_name]:
            with open(round_dir / 'submissions_history.json', encoding='utf-8') as f:
                submit_history = json.load(f)

            if submit_name not in submit_history:
                raise Exception(f"No submitter named `{submit_name}` in {round_metadata['round_name']}.")

            submission_idx = next((i for i, (timestamp, *_) in enumerate(submit_history[submit_name])
                                   if timestamp == submit_time),
                                  None)
            if submission_idx is None:
                raise Exception(f"No submission with timestamp `{submit_time}`"
                                f" in {round_metadata['round_name']} scoring submission history")

            del submit_history[submit_name][submission_idx]

            # If this was their latest submission under their current submit name, remove it from solutions.txt
            if submission_idx == len(submit_history[submit_name]):  # No -1 as history length just decreased by 1
                # Note that if we just removed a submission of theirs or if this was their last solo submission
                # before being put in a team, solutions.txt may still be empty, in which case we should not DM them
                with open(round_dir / 'solutions.txt', encoding='utf-8') as f:
                    solns_str = f.read()

                soln_strs = list(schem.Solution.split_solutions(solns_str))
                soln_idx = next((i for i, soln_str in enumerate(soln_strs)
                                 if schem.Solution.parse_metadata(soln_str)[1] == submit_name),
                                None)

                if soln_idx is not None:
                    assert reason is not None, "`reason` arg must be given if submission is player's latest"

                    del soln_strs[soln_idx]
                    with open(round_dir / 'solutions.txt', 'w', encoding='utf-8') as f:
                        f.write('\n'.join(soln_strs))

                    # Find the ID's of all players we need to DM
                    with open(tournament_dir / 'participants.json', encoding='utf-8') as f:
                        players = json.load(f)
                    with open(round_dir / 'teams.json', encoding='utf-8') as f:
                        teams = json.load(f)

                    if submit_name in teams:
                        affected_discord_ids.update(players[tag]['id'] for tag in teams[submit_name])
                    else:
                        # If next() fails below there'd be a ghost submission so I'm happy to let it
                        affected_discord_ids.add(next(d['id'] for tag, d in players.items()
                                                      if 'name' in d and d['name'] == submit_name))

                    # DM the relevant players
                    for discord_id in affected_discord_ids:
                        user = await self.bot.fetch_user(discord_id)
                        await user.send(
                            f"The TO has deleted your scoring submission to {round_metadata['round_name']} (`{puzzle_name}`)."
                            f'\nReason: "{reason}"'
                            "\nPlease submit a new scoring solution accordingly.")

            with open(round_dir / 'submissions_history.json', 'w', encoding='utf-8') as f:
                json.dump(submit_history, f, ensure_ascii=False, indent=4)

        if affected_discord_ids:
            await ctx.send("Removed submission from history and solutions.txt, and DM'd "
                           + ', '.join(f'<@{d_id}>' for d_id in affected_discord_ids)
                           + " to inform them.",
                           allowed_mentions=discord.AllowedMentions(users=False))
        else:
            await ctx.send("Removed submission from history.")
