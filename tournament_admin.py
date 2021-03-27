#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import asyncio
from datetime import datetime, timezone
import json
import shutil

import discord
from discord.ext import commands
import schem
from slugify import slugify

from metric import validate_metric, validate_metametric
from tournament_base import PuzzleSubmissionsLock, BaseTournament, ANNOUNCEMENTS_CHANNEL_ID, is_tournament_host
from utils import process_start_end_dates, format_date

# TODO: Organize things to be able to use the same commands or code to run standalone puzzle challenges unrelated to
#       any tournament, e.g. puzzle-of-the-week/month, behaving like a standalone tournament round


class TournamentAdmin(BaseTournament):
    """Admin-level tournament commands."""

    # Decorator for host-only commands
    is_host = commands.check(is_tournament_host)

    def hosts(self):
        """Return a set of the users with tournament-hosting permissions."""
        hosts_json_file = self.TOURNAMENTS_DIR / 'hosts.json'
        if not hosts_json_file.exists():
            return set()

        with open(hosts_json_file, encoding='utf-8') as f:
            return set(json.load(f)['hosts'])

    # Note: Command docstrings should be limited to ~80 char lines to avoid ugly wraps in any reasonably-sized window
    @commands.command(name='tournament-hosts', aliases=['th', 'tournament-host-list', 'thl'])
    @is_host
    async def hosts_cmd(self, ctx):
        """List all tournament hosts."""
        await ctx.send(f"The following users have tournament-hosting permissions: {', '.join(self.hosts())}")

    @commands.command(name='tournament-host-add', aliases=['tournament-add-host', 'add-tournament-host'])
    @commands.is_owner()
    #@commands.dm_only()
    async def add_tournament_host(self, ctx, user: discord.User):
        """Give someone tournament-hosting permissions."""
        discord_tag = str(user)  # e.g. <username>#1234. Guaranteed to be unique

        self.TOURNAMENTS_DIR.mkdir(exist_ok=True)

        hosts = self.hosts()
        if discord_tag in hosts:
            raise ValueError("Given user is already a tournament host")
        hosts.add(discord_tag)

        with open(self.TOURNAMENTS_DIR / 'hosts.json', 'w', encoding='utf-8') as f:
            json.dump({'hosts': list(hosts)}, f)

        await ctx.send(f"{discord_tag} added to tournament hosts.")

    @commands.command(name='tournament-host-remove', aliases=['tournament-remove-host', 'remove-tournament-host'])
    @commands.is_owner()
    #@commands.dm_only()
    async def remove_tournament_host(self, ctx, user: discord.User):
        """Remove someone's tournament-hosting permissions."""
        discord_tag = str(user)  # e.g. <username>#1234. Guaranteed to be unique

        hosts = self.hosts()
        if discord_tag not in hosts:
            raise ValueError("Given user is not a tournament host")
        hosts.remove(discord_tag)

        with open(self.TOURNAMENTS_DIR / 'hosts.json', 'w', encoding='utf-8') as f:
            json.dump({'hosts': list(hosts)}, f, ensure_ascii=False, indent=4)

        await ctx.send(f"{discord_tag} removed from tournament hosts.")

    @commands.command(name='tournament-create', aliases=['tc'])
    @is_host
    #@commands.dm_only()
    async def tournament_create(self, ctx, name, start, end, metametric='best_metric / your_metric'):
        """Create a tournament.

        There may only be one tournament pending/active at a time.

        name: The tournament's official name, e.g. "2021 SpaceChem Tournament"
        start: The datetime on which the bot will announce the tournament publicly and
               after which puzzle rounds may start. ISO format, default UTC.
               E.g. the following are all equivalent: 2000-01-31, "2000-01-31 00:00",
                    2000-01-30T19:00:00-05:00
        end: The datetime on which the bot will announce the tournament results,
             after closing and tallying the results of any still-open puzzles.
             Same format as `start`.
        metametric: The equation determining how much of a puzzle's points each
                    player will receive (highest metametric receives full puzzle points).
                    Valid terms:
                        your_metric, best_metric, your_rank_idx, num_solvers
                        where your_rank_idx is the rank, 0-indexed (with ties possible,
                        e.g. 0, 1, 1, 3, 4, ...).
                    The metametric will be auto-normalized to give each player
                    puzzle_points * (your_metametric / best_metametric) points.
                    E.g. "4*(best_metric / your_metric) + (your_rank_idx / num_submitters)"
                    would split the weight of metric vs placement 80-20.
        """
        tournament_dir_name = slugify(name)  # Convert to a valid directory name
        assert tournament_dir_name, f"Invalid tournament name {name}"

        validate_metametric(metametric)
        start, end = process_start_end_dates(start, end)

        self.TOURNAMENTS_DIR.mkdir(exist_ok=True)

        async with self.tournament_metadata_write_lock:
            if self.ACTIVE_TOURNAMENT_FILE.exists():
                raise FileExistsError("There is already an active or upcoming tournament.")

            tournament_dir = self.TOURNAMENTS_DIR / tournament_dir_name
            tournament_dir.mkdir(exist_ok=False)

            with open(self.ACTIVE_TOURNAMENT_FILE, 'w', encoding='utf-8') as f:
                f.write(tournament_dir_name)

            # Initialize tournament metadata, participants, and standings files
            tournament_metadata = {'name': name, 'host': ctx.message.author.name,
                                   'metametric': metametric,
                                   'start': start, 'end': end, 'rounds': {}}
            with open(tournament_dir / 'tournament_metadata.json', 'w', encoding='utf-8') as f:
                json.dump(tournament_metadata, f, ensure_ascii=False, indent=4)

            with open(tournament_dir / 'participants.json', 'w', encoding='utf-8') as f:
                json.dump({}, f, ensure_ascii=False, indent=4)

            with open(tournament_dir / 'standings.json', 'w', encoding='utf-8') as f:
                json.dump({'rounds': {}, 'total': {}}, f, ensure_ascii=False, indent=4)

            # Schedule the tournament announcement
            self.tournament_start_task = self.bot.loop.create_task(self.announce_tournament_start(tournament_metadata))

        await ctx.send(f"Successfully created {repr(name)}")

    @commands.command(name='tournament-update', aliases=['tournament-edit'])
    @is_host
    #@commands.dm_only()
    async def tournament_update(self, ctx, *update_fields):
        """Update the current/pending tournament.

        If the tournament is already open, a post announcing the updated fields will
        be made.

        update_fields: Fields to update, specified like:
                       field1=value "field2=value with spaces"
                       Valid fields (same as tournament-create): name, start, end
                       Double-quotes within a field's value should be avoided
                       as they will interfere with arg-parsing.
                       Unspecified fields will not be modified.
        E.g.: !tournament-update "name=2000 SpaceChem Tournament" end=2000-01-31T19:00-05:00
        """
        async with self.tournament_metadata_write_lock:
            tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata(is_host=True)

            if 'end_post' in tournament_metadata:
                raise Exception("Cannot edit closed tournament.")

            parser = argparse.ArgumentParser(exit_on_error=False)
            parser.add_argument('--name')
            parser.add_argument('--metametric')
            parser.add_argument('--start', '--start_date')
            parser.add_argument('--end', '--end_date')

            # Heavy-handed SystemExit catch because even with exit_on_error, unknown args can cause an exit:
            # https://bugs.python.org/issue41255
            try:
                args = parser.parse_args(f'--{s}' for s in update_fields)
            except SystemExit:
                raise Exception("Unrecognized arguments included, double-check `!help tournament-update`")

            updated_fields = set(k for k, v in vars(args).items() if v)
            assert updated_fields, "Please specify field(s) to update"

            # Prepare a text post summarizing the changed fields
            # TODO: @tournament or some such
            summary_text = "**The host has updated the current tournament!**\nChanges:"

            modified_round_ends = set()
            modified_open_round_ends = set()

            if args.metametric:
                validate_metametric(args.metametric)

            if args.start or args.end:
                # Reformat and do basic checks on any changed date args (e.g. making sure end is after start)
                # If the start date was not changed skip the check for it being in the future
                start, end = process_start_end_dates(args.start if args.start else tournament_metadata['start'],
                                                     args.end if args.end else tournament_metadata['end'],
                                                     check_start_in_future=bool(args.start))
                if args.start:
                    args.start = start

                    assert 'start_post' not in tournament_metadata, "Cannot update start date; tournament is already open!"

                    # Check that this doesn't violate any puzzle start dates
                    for round_metadata in tournament_metadata['rounds'].values():
                        if args.start > round_metadata['start']:  # Safe since we convert everything to ISO and UTC
                            raise ValueError(f"New start date is after start of {repr(round_metadata['round_name'])}")

                if args.end:
                    args.end = end

                    # Change the end date of any puzzles that ended at the same time as the tournament
                    # For all other puzzles, check their end dates weren't violated
                    for puzzle_name, round_metadata in tournament_metadata['rounds'].items():
                        # Again all below date comparisons safe since everything is ISO and UTC format
                        if round_metadata['end'] == tournament_metadata['end']:
                            # Check round start isn't violated before we modify round end
                            if round_metadata['start'] >= args.end:
                                raise ValueError(f"New end date is before start of `{round_metadata['round_name']}`")

                            round_metadata['end'] = args.end

                            modified_round_ends.add(round_metadata['round_name'])
                            if 'start_post' in round_metadata:
                                modified_open_round_ends.add(round_metadata['round_name'])

                        elif args.end < round_metadata['end']:
                            raise ValueError(f"New end date is before end of `{round_metadata['round_name']}`")

            # Update tournament metadata and summary post
            for k, v in vars(args).items():
                if v:
                    # Check that field has actually changed
                    if v == tournament_metadata[k]:
                        raise ValueError(f"{k} was already `{v}`, did you mean to update it?")

                    if k in ('start', 'end'):
                        summary_text += f"\n  • {k}: `{format_date(tournament_metadata[k])}` -> `{format_date(v)}`"
                    else:
                        summary_text += f"\n  • {k}: `{tournament_metadata[k]}` -> `{v}`"
                    tournament_metadata[k] = v

            # Mention any open puzzles that were edited in the public summary text
            if modified_open_round_ends:
                summary_text += f"\n    {', '.join(f'`{s}`' for s in modified_open_round_ends)}" \
                                + " had end date modified to match new tournament end date."

            # If tournament is already open, ask for confirmation before making changes
            if 'start_post' in tournament_metadata:
                await ctx.send("The tournament is already open so the following public announcement post will be made:"
                               "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                await ctx.send(summary_text)
                confirm_msg = await ctx.send("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                                             "\nAre you sure you wish to continue?"
                                             " React with ✅ within 30 seconds to proceed, ❌ to cancel all changes.")
                if not await self.wait_for_confirmation(ctx, confirm_msg):
                    return

            # Update name last so the directory rename won't occur if other args were invalid
            if args.name:
                new_tournament_dir_name = slugify(args.name)
                assert new_tournament_dir_name, f"Invalid tournament name {args.name}"

                tournament_dir.rename(self.TOURNAMENTS_DIR / new_tournament_dir_name)
                tournament_dir = self.TOURNAMENTS_DIR / new_tournament_dir_name

                with open(self.ACTIVE_TOURNAMENT_FILE, 'w', encoding='utf-8') as f:
                    f.write(new_tournament_dir_name)

            # Save changes to metadata file
            with open(tournament_dir / 'tournament_metadata.json', 'w', encoding='utf-8') as f:
                json.dump(tournament_metadata, f, ensure_ascii=False, indent=4)

            # If the update was successful and changed a date(s), cancel and replace the relevant BG announcement task
            if args.start:
                self.tournament_start_task.cancel()
                self.tournament_start_task = self.bot.loop.create_task(self.announce_tournament_start(tournament_metadata))
            elif args.end and self.tournament_results_task is not None:
                self.tournament_results_task.cancel()
                self.tournament_results_task = self.bot.loop.create_task(self.announce_tournament_results(tournament_metadata))

        # If the tournament was open, announce the changes publicly and update any puzzle end dates
        if 'start_post' in tournament_metadata:
            # Update any rounds that had their end date changed to match the tournament end date
            channel = self.bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
            for puzzle_name, round_metadata in tournament_metadata['rounds'].items():
                if round_metadata['round_name'] in modified_open_round_ends:
                    # Edit the announcement post
                    msg_id = round_metadata['start_post'].strip('/').split('/')[-1]
                    announcement_msg = await channel.fetch_message(msg_id)
                    announcement_embed = self.round_announcement(tournament_dir, tournament_metadata, puzzle_name)[0]
                    await announcement_msg.edit(embed=announcement_embed)

                    # Update the results announcement task
                    self.round_results_tasks[puzzle_name].cancel()
                    self.tournament_results_task = self.bot.loop.create_task(self.announce_round_results(puzzle_name, round_metadata))

            channel = self.bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
            await channel.send(summary_text)

        # Regardless of any public posts, inform the TO of all changes made (including those to future puzzles)
        reply = f"Successfully updated tournament {', '.join(updated_fields)}."
        if modified_round_ends:
            reply += f"\nEnd date of round(s) `{'`, `'.join(modified_round_ends)}` updated to match new end date."

        await ctx.send(reply)

    # TODO: @commands.command(name='tournament-delete')  # Is this existing too dangerous?
    #                                                    # In any case tournament-update should be sufficient for now

    @staticmethod
    async def read_puzzle_attachment(discord_file):
        if not discord_file.filename.endswith('.puzzle'):
            # TODO: Could fall back to slugify(level.name) or slugify(round_name) for the .puzzle file name if the
            #       extension doesn't match
            raise ValueError("Attached file should use the extension .puzzle")

        level_bytes = await discord_file.read()
        try:
            return level_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise Exception("Attachment must be a plaintext file (containing a level export code).") from e

    # TODO: Puzzle flavour text
    @commands.command(name='tournament-puzzle-add', aliases=['tpa', 'tournament-add-puzzle', 'tap'])
    @is_host
    #@commands.dm_only()
    async def tournament_add_puzzle(self, ctx, round_name, metric, points: float, start, end=None):
        """Add a puzzle to the tournament.

        round_name: e.g. "Round 1" or "Bonus 1".
        metric: The equation a player should minimize.
                A player's final score for the round will be the top metric
                score divided by this metric score.
                Allowed terms: <Any real number>, cycles, reactors, symbols,
                               waldopath, waldos, bonders, arrows, flip_flops,
                               sensors, syncs.
                Allowed operators/fns: ^ (or **), /, *, +, -, max(), min(),
                                       log() (base 10)
                Parsed with standard operator precedence (BEDMAS).
                E.g.: "cycles + 0.1 * symbols + bonders^2"
        points: # of points that the first place player will receive.
                Other players will get points proportional to this based
                on their relative metric score.
        start: The datetime on which the puzzle will be announced and submissions opened.
               ISO format, default UTC.
               E.g. the following are all equivalent: 2000-01-31, "2000-01-31 00:00",
                    2000-01-30T19:00:00-05:00
        end: The datetime on which submissions will close and the results will be
             announced. Same format as `start`.
             If excluded, puzzle is open until the tournament is ended (e.g. the
             2019 tournament's 'Additional' puzzles).
        """
        # Check attached puzzle
        assert len(ctx.message.attachments) == 1, "Expected one attached puzzle file!"
        puzzle_file = ctx.message.attachments[0]
        level_code = await self.read_puzzle_attachment(puzzle_file)
        level = schem.Level(level_code)

        validate_metric(metric)

        async with self.tournament_metadata_write_lock:
            tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata(is_host=True)

            # Check if either of puzzle/round name are too similar to existing names of either type (since e.g.
            # tournament-info searches case-insensitively for either name)
            for name in (level.name, round_name):
                if self.get_puzzle_name(tournament_metadata, name, is_host=True, missing_ok=True) is not None:
                    raise ValueError(f"Puzzle/round with name ~= `{name}` already exists in the current tournament")
            round_dir_name = f'{slugify(round_name)}_{slugify(level.name)}'  # Human-friendly directory name

            # Validate start/end datetimes
            if end is None:
                end = tournament_metadata['end']

            start, end = process_start_end_dates(start, end)  # Format and basic temporal sanity checks

            # Also check against the tournament start/end dates
            # String comparisons are safe here because all datetimes have been converted to ISO + UTC format
            if start < tournament_metadata['start']:
                raise ValueError(f"Round start time is before tournament start ({tournament_metadata['start']}).")
            elif end > tournament_metadata['end']:
                raise ValueError(f"Round end time is after tournament end ({tournament_metadata['end']}).")

            tournament_metadata['rounds'][level.name] = {'dir': round_dir_name,
                                                         'round_name': round_name,
                                                         'metric': metric,
                                                         'points': points,
                                                         'start': start,
                                                         'end': end}

            # Re-sort rounds by start date
            tournament_metadata['rounds'] = dict(sorted(tournament_metadata['rounds'].items(),
                                                        key=lambda x: x[1]['start']))

            # Set up the round directory
            round_dir = tournament_dir / round_dir_name
            round_dir.mkdir(exist_ok=False)
            await puzzle_file.save(round_dir / puzzle_file.filename)
            (round_dir / 'solutions.txt').touch()
            (round_dir / 'solutions_fun.txt').touch()

            with open(tournament_dir / 'tournament_metadata.json', 'w', encoding='utf-8') as f:
                json.dump(tournament_metadata, f, ensure_ascii=False, indent=4)

            # Schedule the round announcement (if the start announcement task has already run and won't do it for us)
            if 'start_post' in tournament_metadata:
                self.round_start_tasks[level.name] = \
                    self.bot.loop.create_task(self.announce_round_start(level.name, tournament_metadata['rounds'][level.name]))

            # TODO: Track the history of each player's scores over time and do cool graphs of everyone's metrics going
            #       down as the deadline approaches!
            #       Can do like the average curve of everyone's scores over time and see how that curve varies by level
            #       Probably don't store every solution permanently to avoid the tournament.zip getting bloated but can
            #       at least keep the scores from replaced solutions.

            # TODO 2: Pareto frontier using the full submission history!

        await ctx.send(f"Successfully added {round_name} {level.name} to {tournament_metadata['name']}")

    async def wait_for_confirmation(self, ctx, confirm_msg, confirm_react='✅', cancel_react='❌', timeout_seconds=30):
        """Wait for a reaction to the given message confirming an operation (by the user who created the passed
        context), returning True if they confirm and False otherwise. If the message is cancelled or the given timeout
        is reached, also send a message in the given context indicating the operation was cancelled.
        """
        def check(reaction_event):
            return (reaction_event.message_id == confirm_msg.id
                    and reaction_event.user_id == ctx.message.author.id
                    and str(reaction_event.emoji) in (confirm_react, cancel_react))

        try:
            # reaction_add doesn't work in DMs without the `members` intent given to the Bot constructor, which we don't
            # really need (see https://discordpy.readthedocs.io/en/latest/api.html#discord.on_reaction_add)
            reaction_event = await self.bot.wait_for('raw_reaction_add', timeout=timeout_seconds, check=check)

            if str(reaction_event.emoji) == confirm_react:
                return True
            else:
                await ctx.send('Operation cancelled!')
                return False
        except asyncio.TimeoutError:
            await ctx.send('Operation cancelled!')
            return False

    @commands.command(name='tournament-puzzle-update', aliases=['tournament-puzzle-edit', 'tournament-update-puzzle',
                                                                'tournament-edit-puzzle'])
    @is_host
    #@commands.dm_only()
    async def update_puzzle(self, ctx, round_or_puzzle_name, *update_fields):  # TODO: public_explanation_blurb
        """Update the specified puzzle.

        If the puzzle is already open, a post announcing the updated fields will
        be made and the original announcement post edited.

        If the puzzle file is also updated, the following will also occur:
            - Player solutions will be re-validated, and any invalidated
              solutions will be removed and their authors DM'd to inform
              them of this.
            - As attachments cannot be edited/deleted, instead of editing
              the original announcement post, a new announcement post will
              be made (after the change summary post), and linked to from
              the old announcement post.

        round_or_puzzle_name: (Case-insensitive) Round or puzzle to update.
                              May not be a closed puzzle.
        update_fields: Fields to update, specified like:
                       field1=value "field2=value with spaces"
                       Valid fields (same as tournament-add-puzzle):
                           round_name, metric, points, start, end
                       Double-quotes within a field's value should be avoided
                       as they will interfere with arg-parsing.
                       Unspecified fields will not be modified.
                       A new puzzle file may also be attached.
        E.g.: !tournament-puzzle-update "Round 3" "round_name=Round 3.5" end=2000-01-31T19:17-05:00
        """
        async with self.tournament_metadata_write_lock:
            tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata(is_host=True)
            puzzle_name = self.get_puzzle_name(tournament_metadata, round_or_puzzle_name, is_host=True, missing_ok=False)
            round_metadata = tournament_metadata['rounds'][puzzle_name]

            if 'end_post' in round_metadata:
                raise Exception("Cannot edit closed puzzle.")

            parser = argparse.ArgumentParser(exit_on_error=False)
            parser.add_argument('--round_name')
            parser.add_argument('--metric')
            parser.add_argument('--points', type=float)
            parser.add_argument('--start', '--start_date')
            parser.add_argument('--end', '--end_date')

            # Heavy-handed SystemExit catch because even with exit_on_error, unknown args can cause an exit:
            # https://bugs.python.org/issue41255
            try:
                args = parser.parse_args(f'--{s}' for s in update_fields)
            except SystemExit:
                raise Exception("Unrecognized arguments included, double-check `!help tournament-puzzle-update`")

            args_dict = vars(args)
            updated_fields = set(k for k, v in args_dict.items() if v)

            assert updated_fields or ctx.message.attachments, "Please specify field(s) to update or attach new puzzle file"

            # Check that only changed fields were specified
            for k, v in args_dict.items():
                if v and v == round_metadata[k]:
                    raise ValueError(f"{k} was already `{v}`, did you mean to update it?")

            assert not args.start or 'start_post' not in round_metadata, "Cannot update start date; puzzle is already open!"

            # Make sure new round name doesn't conflict with any existing ones
            old_round_name = round_metadata['round_name']
            if (args.round_name
                    and self.get_puzzle_name(tournament_metadata, args.round_name,
                                             is_host=True, missing_ok=True) is not None):
                raise ValueError(f"Puzzle/round with name ~= `{args.round_name}` already exists in the current tournament")

            # Reformat and do basic checks on any changed date args (e.g. making sure end is after start)
            if args.start or args.end:
                # If the start date was not changed skip the check for it being in the future
                start, end = process_start_end_dates(args.start if args.start else round_metadata['start'],
                                                     args.end if args.end else round_metadata['end'],
                                                     check_start_in_future=bool(args.start))
                if args.start:
                    args.start = start

                if args.end:
                    args.end = end

            if args.metric:
                validate_metric(args.metric)

            # Prepare a text post summarizing the changed fields
            # TODO: @tournament or some such
            summary_text = (f"**The tournament host has updated {round_metadata['round_name']}, {puzzle_name}**"
                            + "\nChanges:")

            # Update round metadata and summary text
            for k, v in vars(args).items():  # Re-fetch args dict since start/end might be reformatted
                if v:
                    if k in ('start', 'end'):
                        summary_text += f"\n  • {k}: `{format_date(round_metadata[k])}` -> `{format_date(v)}`"
                    else:
                        summary_text += f"\n  • {k}: `{round_metadata[k]}` -> `{v}`"
                    round_metadata[k] = v

            try:
                if ctx.message.attachments:
                    assert len(ctx.message.attachments) == 1, "Expected at most a single attached puzzle file!"
                    new_puzzle_file = ctx.message.attachments[0]
                    new_level_code = (await self.read_puzzle_attachment(new_puzzle_file)).strip().replace("\r\n", "\n")
                    level = schem.Level(new_level_code)

                    # Make sure the new puzzle name doesn't conflict with any other rounds/puzzles
                    if not (self.get_puzzle_name(tournament_metadata, level.name,
                                                 is_host=True, missing_ok=True) in (None, puzzle_name)):
                        raise ValueError(f"Puzzle/round with name ~= `{level.name}` already exists in the current tournament")

                    # Update the puzzle name in metadata. We'll leave the directory name unchanged until after confirmation
                    new_puzzle_name = level.name
                    del tournament_metadata['rounds'][puzzle_name]
                    tournament_metadata['rounds'][new_puzzle_name] = round_metadata

                    updated_fields.add("puzzle file")
                    summary_text += ("\n  • Puzzle file changed."
                                     "\n    Any players whose solutions were invalidated by this change have been DM'd.")

                    # Double-check that the puzzle code actually changed
                    round_dir = tournament_dir / round_metadata['dir']
                    old_puzzle_file = next(round_dir.glob('*.puzzle'), None)
                    assert old_puzzle_file is not None, "Internal Error: puzzle file for specified round is missing"
                    with open(old_puzzle_file, 'r', encoding='utf-8') as f:
                        old_level_code = f.read().strip()
                    old_level = schem.Level(old_level_code)
                    assert level != old_level, "Attached puzzle has not changed, did you mean to update it?"

                    # First check whether this puzzle file will invalidate any solutions
                    if 'start_post' in round_metadata:
                        # Wait for current submitters to finish adding solutions then temporarily block new submitters
                        await self.puzzle_submission_locks[puzzle_name].lock_and_wait_for_submitters()

                        msg = await ctx.send("Re-validating player submissions, this may take a few minutes...")
                        loop = asyncio.get_event_loop()
                        invalid_soln_authors = set()
                        valid_soln_strs = {}

                        for solns_file_name in ('solutions.txt', 'solutions_fun.txt'):
                            solns_file = round_dir / solns_file_name
                            if not solns_file.is_file():
                                continue

                            valid_soln_strs[solns_file_name] = []
                            with open(solns_file, 'r', encoding='utf-8') as f:
                                solns_str = f.read()

                            for soln_str in schem.Solution.split_solutions(solns_str):
                                _, author_name, _, _ = schem.Solution.parse_metadata(soln_str)

                                # Call the SChem validator in a thread so the bot isn't blocked
                                # TODO: If/when 'outputs' is a metric term, will need to update this similarly to submit to
                                #       allow partial solutions in its presence
                                try:
                                    solution = schem.Solution(level, soln_str)
                                    await loop.run_in_executor(None, solution.validate)
                                except Exception:
                                    invalid_soln_authors.add(author_name)
                                    continue

                                # TODO: If the solution string was still valid and the level name changed, update the
                                #       solution's level name

                                valid_soln_strs[solns_file_name].append(soln_str)

                        # Prepare a new announcement post and the puzzle file to attach
                        # Pass the attached puzzle file instead of using the round's
                        # TODO: This is pr hacky, maybe should separate the attachment generation from round_announcement
                        new_announcement_embed, new_announcement_attachment = \
                            self.round_announcement(tournament_dir, tournament_metadata, new_puzzle_name,
                                                    level_code=new_level_code, attachment=(await new_puzzle_file.to_file()))
                else:
                    assert updated_fields, "Missing fields to update or puzzle file attachment!"
                    new_puzzle_name = puzzle_name

                    # If the round is open but we don't need to change the puzzle file, prepare an edit to the original
                    # announcements post
                    if 'start_post' in round_metadata:
                        # Update the announcement using the modified tournament metadata
                        edited_announcement_embed = self.round_announcement(tournament_dir, tournament_metadata, new_puzzle_name)[0]

                if 'start_post' in round_metadata:
                    msg_id = round_metadata['start_post'].strip('/').split('/')[-1]
                    channel = self.bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
                    og_announcement = await channel.fetch_message(msg_id)

                    # Preview the changes-summary post, the new or edited announcement post, and the names of all players
                    # whose solutions were invalidated, and ask for TO confirmation
                    if ctx.message.attachments:
                        # Edit the 'running solutions...' message
                        await msg.edit(content="The specified puzzle has already been opened so the following public"
                                               " announcement posts will be made:\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                        await ctx.send(summary_text)
                        await ctx.send(embed=new_announcement_embed, file=new_announcement_attachment)
                        edit_note_text = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" \
                                         + "The original announcement post will be edited to include a link to the above post."
                        if invalid_soln_authors:
                            edit_note_text += "\n**Additionally, this change to the puzzle invalidated the following players'" \
                                              + f" solutions**: {', '.join(invalid_soln_authors)}" \
                                              + "\nThese players will be DM'd to inform them of their removed solution(s)."
                        else:
                            edit_note_text += "\n**No player solutions were invalidated by this puzzle file change.**"
                        await ctx.send(edit_note_text)
                    else:
                        await ctx.send("The specified puzzle has already been opened so the following public announcement"
                                       " post will be made:\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                        await ctx.send(summary_text)
                        await ctx.send("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                                       "\nand the original announcement post will be edited to read:")
                        await ctx.send(embed=edited_announcement_embed,
                                       file=(await og_announcement.attachments[0].to_file()))
                        await ctx.send("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

                    # Ask the TO for confirmation before making any changes
                    confirm_msg = await ctx.send("Are you sure you wish to continue?"
                                                 " React with ✅ within 30 seconds to proceed, ❌ to cancel all changes.")
                    if not await self.wait_for_confirmation(ctx, confirm_msg):
                        return

                    # If the round directory will be renamed, wait for any submitters to finish modifying its contents
                    # if we haven't already
                    if args.round_name and not ctx.message.attachments:
                        await self.puzzle_submission_locks[puzzle_name].lock_and_wait_for_submitters()

                    if ctx.message.attachments:
                        # Save the new puzzle file
                        old_puzzle_file.unlink()
                        await new_puzzle_file.save(round_dir / new_puzzle_file.filename)

                        # Update solutions.txt and fun_solutions.txt
                        for solns_file_name, cur_soln_strs in valid_soln_strs.items():
                            with open(round_dir / solns_file_name, 'w', encoding='utf-8') as f:
                                f.write('\n'.join(cur_soln_strs))

                    # Make the changes-summary post and edit the original announcement post or make the new post if
                    # the puzzle file changed
                    msg_id = round_metadata['start_post'].strip('/').split('/')[-1]
                    og_announcement = await channel.fetch_message(msg_id)
                    if ctx.message.attachments:
                        # Make the changes-summary post
                        await channel.send(summary_text + "\n\nNew announcement post:")

                        msg = await channel.send(embed=new_announcement_embed, file=(await new_puzzle_file.to_file()))
                        await og_announcement.edit(
                            content=f"**EDIT: This puzzle has been updated, see the new announcement post here**: {msg.jump_url}")
                        round_metadata['start_post'] = msg.jump_url
                    else:
                        await og_announcement.edit(embed=edited_announcement_embed)

                        await channel.send(summary_text
                                           + f"\n\nThe announcement post has been edited: {og_announcement.jump_url}")

                    # Create a new (open) submissions lock
                    del self.puzzle_submission_locks[puzzle_name]
                    self.puzzle_submission_locks[new_puzzle_name] = PuzzleSubmissionsLock()
                elif ctx.message.attachments:
                    # Save the new puzzle file and update the tournament metadata's puzzle name as needed
                    old_puzzle_file.unlink()
                    await new_puzzle_file.save(round_dir / new_puzzle_file.filename)
                    del tournament_metadata['rounds'][puzzle_name]
                    tournament_metadata['rounds'][new_puzzle_name] = round_metadata
            finally:
                # Make sure we re-unlock the puzzle even if the process was rejected or was cancelled
                # Note that this will have no effect if the lock was already restored or has changed names
                if puzzle_name in self.puzzle_submission_locks:
                    self.puzzle_submission_locks[puzzle_name].unlock()

            # Update and move the round directory
            old_round_dir = tournament_dir / round_metadata['dir']
            round_metadata['dir'] = f"{slugify(round_metadata['round_name'])}_{slugify(new_puzzle_name)}"
            shutil.move(old_round_dir, tournament_dir / round_metadata['dir'])

            # Update the tournament metadata
            with open(tournament_dir / 'tournament_metadata.json', 'w', encoding='utf-8') as f:
                json.dump(tournament_metadata, f, ensure_ascii=False, indent=4)

            # Replace the relevant announcement task if a date changed
            if 'start' in updated_fields and puzzle_name in self.round_start_tasks:
                self.round_start_tasks[puzzle_name].cancel()
                del self.round_start_tasks[puzzle_name]
                self.round_start_tasks[new_puzzle_name] = self.bot.loop.create_task(self.announce_round_start(puzzle_name, round_metadata))

            if 'end' in updated_fields and puzzle_name in self.round_results_tasks:
                self.round_results_tasks[puzzle_name].cancel()
                del self.round_results_tasks[puzzle_name]
                self.round_results_tasks[new_puzzle_name] = self.bot.loop.create_task(self.announce_round_results(puzzle_name, round_metadata))

            # DM any players whose solutions were invalidated
            if 'start_post' in round_metadata and ctx.message.attachments and invalid_soln_authors:
                with open(tournament_dir / 'participants.json', 'r', encoding='utf-8') as f:
                    participants = json.load(f)

                # Construct a name:id dict for quicker lookups by name
                # TODO: This won't play nice with teams, need name -> list_of_ids
                name_to_id = {name: id for id, name in participants.values()}
                non_discord_players = set()

                for player_name in invalid_soln_authors:
                    if player_name in name_to_id:
                        user = await self.bot.fetch_user(name_to_id[player_name])
                        await user.send(
                            f"{old_round_name}, {puzzle_name} has been updated and one or more of your submissions"
                            " were invalidated by the change! Please check"
                            f' `!tournament-list-submissions {round_metadata["round_name"]}` and update/re-submit'
                            " any missing solutions as needed.")
                    else:
                        non_discord_players.add(player_name)

                # Warn the TO of any solutions for whom the authors couldn't be found on discord (e.g. added by the
                # TO submit backdoor)
                if non_discord_players:
                    await ctx.send("Warning: The following authors could not be DM'd about their invalid submissions"
                                   " since they have no associated discord account (you probably submitted for them):"
                                   ", ".join(f"`{name}`" for name in non_discord_players) + "."
                                                                                            "\nConsider contacting these players to inform them of their invalidated solution(s).")

        await ctx.send(f"Updated {', '.join(updated_fields)} for {round_metadata['round_name']}, {puzzle_name}")

    @commands.command(name='tournament-puzzle-delete', aliases=['tournament-delete-puzzle'])
    @is_host
    #@commands.dm_only()
    async def delete_puzzle(self, ctx, *, round_or_puzzle_name):
        """Delete a round/puzzle.

        round_or_puzzle_name: (Case-insensitive) If provided, show only your submissions
                              to the specified round/puzzle. May be a past puzzle.
        """
        async with self.tournament_metadata_write_lock:
            tournament_dir, tournament_metadata = self.get_active_tournament_dir_and_metadata(is_host=True)

            # Convert to puzzle name
            puzzle_name = self.get_puzzle_name(tournament_metadata, round_or_puzzle_name,
                                               is_host=True, missing_ok=False)

            round_metadata = tournament_metadata['rounds'][puzzle_name]
            round_dir = tournament_dir / round_metadata['dir']
            round_name = round_metadata['round_name']

            msg = None

            # Ask for confirmation before deleting if the round start date has passed
            if datetime.now(timezone.utc).isoformat() > round_metadata['start']:
                timeout_seconds = 30
                warn_msg = await ctx.send(
                    f"Warning: This round's start date ({format_date(round_metadata['start'])}) has already passed"
                    + " and deleting it will delete any player solutions. Are you sure you wish to continue?"
                    + f"\nReact to this message with ✅ within {timeout_seconds} seconds to delete anyway, ❌ to cancel.")

                if not await self.wait_for_confirmation(ctx, warn_msg):
                    return

                if puzzle_name in self.puzzle_submission_locks:
                    msg = await ctx.send("Waiting for any current submitters...")
                    await self.puzzle_submission_locks[puzzle_name].lock_and_wait_for_submitters()
                    del self.puzzle_submission_locks[puzzle_name]

            # Subtract this puzzle from the tournament standings if its results have already been tallied
            if 'end_post' in round_metadata:
                with open(tournament_dir / 'standings.json', 'r', encoding='utf-8') as f:
                    standings = json.load(f)

                for player, points in standings['rounds'][puzzle_name].items():
                    if player in standings['total']:  # Check needed due to 0-scores being excluded from total
                        standings['total'][player] -= points
                        if standings['total'][player] == 0:
                            del standings['total'][player]
                del standings['rounds'][puzzle_name]

                with open(tournament_dir / 'standings.json', 'w', encoding='utf-8') as f:
                    json.dump(standings, f, ensure_ascii=False, indent=4)

            # Remove the round directory and metadata
            shutil.rmtree(round_dir)
            del tournament_metadata['rounds'][puzzle_name]

            with open(tournament_dir / 'tournament_metadata.json', 'w', encoding='utf-8') as f:
                json.dump(tournament_metadata, f, ensure_ascii=False, indent=4)

            # Cancel and remove the relevant BG announcement task for the puzzle if any
            if puzzle_name in self.round_start_tasks:
                self.round_start_tasks[puzzle_name].cancel()
                del self.round_start_tasks[puzzle_name]
            elif puzzle_name in self.round_results_tasks:
                self.round_results_tasks[puzzle_name].cancel()
                del self.round_results_tasks[puzzle_name]

        reply = f"Successfully deleted {round_name}, `{puzzle_name}`"
        await (ctx.send(reply) if msg is None else msg.edit(content=reply))