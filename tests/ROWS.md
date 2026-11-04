# Where the old tests went

Step 5 of the readability refactor replaced `tests/selftest.sh` (bash), `tests/test-stub.py` and
`tests/test_acp.py` with four Python test files. This table sends each old row to the tests that
now check it, or to the commit that deleted it with the feature it tested. Each moved check was
re-proved: a mutant of the code it guards turns the new test red.

## tests/selftest.sh rows

| Old row | Now |
|---|---|
| 00 | `test_cli.py` CommandLine.test_help_prints_the_usage |
| 01, 02 | `test_cli.py` CommandLine.test_an_unknown_flag_and_no_arguments_are_usage_errors |
| 03, 04 | `test_cli.py` CommandLine.test_an_unknown_provider_is_refused_with_an_error_envelope |
| 04b | `test_cli.py` CommandLine.test_gemini_is_refused_as_any_unknown_provider |
| 05, 06, 06b | `test_cli.py` CommandLine.test_a_missing_task_file_is_refused_with_a_full_envelope |
| 05b | `test_cli.py` CommandLine.test_a_provider_name_must_be_plain_lowercase |
| 05c | `test_cli.py` CommandLine.test_a_flag_after_the_provider_is_named |
| 05d | `test_cli.py` CommandLine.test_an_out_dir_that_cannot_be_made_is_refused |
| 05e | `test_cli.py` CommandLine.test_a_value_flag_with_no_value_is_a_usage_error |
| 07 | `test_cli.py` CommandLine.test_an_out_dir_in_use_is_refused |
| 07b | `test_cli.py` CommandLine.test_a_non_empty_out_dir_is_refused_and_left_alone |
| 109 | `test_cli.py` CommandLine.test_an_opencode_seat_names_provider_slash_model |
| 120 | `test_cli.py` CommandLine.test_send_refusals_come_before_any_launch |
| 78 | `test_cli.py` SeatPrompt.test_the_prompt_is_the_box_the_task_and_the_job_dir_line |
| 145 | `test_cli.py` SeatPrompt.test_two_seats_given_one_task_differ_only_after_it |
| 95 | `test_cli.py` SeatPrompt.test_the_inbox_and_outbox_exist_empty_before_the_seat_starts |
| 39w | `test_cli.py` Probe.test_the_claude_probe_pings_haiku_from_a_neutral_cwd |
| 39q | `test_cli.py` Probe.test_the_opencode_probe_pings_the_cheapest_go_model_at_low |
| 20 | `test_cli.py` Probe.test_providers_not_installed_each_print_a_full_line |
| 20b | `test_cli.py` Probe.test_the_probe_never_writes_through_a_planted_link |
| 156 | `test_cli.py` Probe.test_probes_never_push_to_the_driver |
| 91 | `test_cli.py` Verbs.test_mail_appends_one_driver_line_to_the_inbox |
| 92 | `test_cli.py` Verbs.test_wait_returns_on_new_mail_and_on_exit |
| 179 | `test_cli.py` Verbs.test_every_verb_line_is_compact_json |
| 199 | `test_cli.py` Verbs.test_wait_after_the_wrapper_died_mid_turn_is_incomplete |
| 206 | `test_cli.py` Verbs.test_a_signalled_one_shot_buy_stops_its_seat_first |
| 204 | `test_cli.py` Verbs.test_quota_is_the_read_only_board |
| 135 | `test_cli.py` EventsView.test_the_grok_stream_renders_to_a_few_readable_lines |
| 136 | `test_cli.py` EventsView.test_the_view_is_the_same_whole_or_fed_line_by_line<br>`test_cli.py` EventsView.test_an_input_opencode_names_late_rides_one_line_under_its_call |
| 131 | `test_cli.py` EventsView.test_follow_ends_with_the_seat |
| 188 | `test_cli.py` JobsRoot.test_a_buy_without_out_lands_in_the_jobs_root<br>`test_cli.py` JobsRoot.test_an_out_dir_is_private_and_its_parent_is_not |
| 189 | `test_cli.py` JobsRoot.test_the_sweep_deletes_only_idle_dirs_hands_named |
| 191 | `test_cli.py` JobsRoot.test_a_resume_is_use |
| 192 | `test_cli.py` JobsRoot.test_the_sweep_judges_again_under_the_lock |
| 201 | `test_cli.py` JobsRoot.test_the_sweep_stops_at_a_mount |
| 100 | `test_seat.py` AcpSeats.test_a_grok_seat_runs_four_turns_on_one_agent |
| 101 | `test_seat.py` AcpSeats.test_an_opencode_seat_runs_a_bare_acp_and_sets_its_effort |
| 102 | `test_seat.py` AcpSeats.test_both_wires_speak_the_event_vocabulary |
| 103 | `test_seat.py` AcpSeats.test_the_answer_is_only_the_text_after_the_last_tool |
| 104 | `test_seat.py` AcpSeats.test_a_cancelled_turn |
| 105 | `test_seat.py` AcpSeats.test_one_edit_reported_twice_is_one_file_event |
| 106 | `test_seat.py` AcpSeats.test_usage_counts_reasoning_as_output_on_both_wires |
| 107 | `test_seat.py` AcpSeats.test_a_send_to_a_stopped_grok_seat_resumes_its_session |
| 122 | `test_seat.py` AcpSeats.test_a_send_to_a_stopped_opencode_seat_resumes_its_session |
| 39s | `test_seat.py` AcpSeats.test_an_opencode_seat_runs_on_its_callers_store |
| 146 | `test_seat.py` AcpSeats.test_each_seat_runs_a_private_agent<br>`test_seat.py` CodexSeats.test_each_seat_runs_a_private_app_server |
| 101m | `test_seat.py` AcpLaunch.test_a_session_on_another_model_is_set_to_the_seats |
| 108 | `test_seat.py` AcpLaunch.test_what_the_agent_does_not_list_is_refused_before_any_prompt |
| 55 | `test_seat.py` AcpLaunch.test_a_usage_limit_answering_a_prompt_is_provider_quota_dead |
| 129 | `test_seat.py` AcpLaunch.test_a_missing_login_answering_a_prompt_is_auth_expired |
| 205 | `test_seat.py` AcpLaunch.test_a_stop_during_the_launch |
| 202 | `test_seat.py` SlowLaunch.test_a_launch_slower_than_a_minute_still_acks |
| 110 | `test_seat.py` AcpClose.test_a_stop_mid_turn_reaps_the_agent_and_its_group |
| 111 | `test_seat.py` AcpClose.test_a_deadline_mid_turn_is_a_timeout_that_keeps_its_session |
| 111b | `test_seat.py` AcpClose.test_a_turn_the_agent_cancels_at_the_deadline_is_still_a_timeout |
| 172 | `test_seat.py` AcpClose.test_a_deadline_mid_turn_is_a_timeout_that_keeps_its_session<br>`test_seat.py` AcpClose.test_a_turn_the_agent_cancels_at_the_deadline_is_still_a_timeout |
| 173 | `test_seat.py` AcpClose.test_an_abandoned_opencode_turn_has_its_session_deleted<br>`test_seat.py` AcpSeats.test_an_idle_stop_deletes_no_session<br>`test_seat.py` AcpClose.test_a_stop_mid_turn_reaps_the_agent_and_its_group<br>`test_seat.py` AcpClose.test_a_turn_the_agent_cancels_at_the_deadline_is_still_a_timeout |
| 175 | `test_seat.py` AcpClose.test_an_agent_that_dies_with_an_error_mid_turn_has_its_session_deleted |
| 177 | `test_seat.py` AcpClose.test_a_seat_bought_without_a_timeout_has_no_deadline |
| 112 | `test_seat.py` AcpClose.test_a_send_while_a_turn_runs_queues_behind_it |
| 137 | `test_seat.py` ClaudeSeats.test_a_send_to_a_busy_seat_never_cancels_its_turn<br>`test_seat.py` AcpClose.test_a_send_while_a_turn_runs_queues_behind_it |
| 113 | `test_seat.py` CodexSeats.test_one_app_server_serves_every_turn |
| 114, 158 | `test_seat.py` CodexSeats.test_a_recorded_wire_plays_four_turns_across_a_relaunch |
| 115 | `test_seat.py` CodexSeats.test_file_changes_become_file_events |
| 116 | `test_seat.py` CodexSeats.test_failures_are_typed_from_error_fields_only |
| 117 | `test_seat.py` CodexSeats.test_a_send_while_a_turn_runs_is_steered_into_it |
| 208 | `test_seat.py` CodexSeats.test_one_turn_start_is_in_flight_at_a_time |
| 181 | `test_seat.py` CodexSeats.test_a_send_its_turn_outran_opens_the_next_turn |
| 118 | `test_seat.py` CodexSeats.test_a_stop_mid_turn_closes_it_as_a_deadline_does |
| 178 | `test_seat.py` CodexSeats.test_a_stop_halts_a_cli_that_exits_0_on_a_term |
| 180 | `test_seat.py` CodexSeats.test_a_relaunch_after_a_killed_wrapper_closes_its_turn_first |
| 182, 123, 170 | `test_seat.py` CodexSeats.test_a_deadline_closes_politely_and_leaves_a_resumable_checkpoint |
| 183 | `test_seat.py` CodexSeats.test_a_collaborators_thread_is_not_the_seats |
| 184 | `test_seat.py` CodexSeats.test_a_main_answer_before_the_collaborator_ends_still_answers |
| 185 | `test_seat.py` CodexSeats.test_a_turn_whose_completion_never_comes_runs_until_a_stop |
| 186 | `test_seat.py` CodexSeats.test_an_unauthorized_turn_is_auth_expired |
| 187 | `test_seat.py` CodexSeats.test_a_malformed_line_is_kept_raw_and_skipped |
| 58, 72 | `test_seat.py` CodexSeats.test_a_one_shot_codex_buy_persists_its_thread |
| 70 | `test_seat.py` CodexSeats.test_the_start_ack_states_the_output_contract |
| 94 | `test_seat.py` CodexSeats.test_an_answer_followed_by_a_tool_is_voided |
| 144 | `test_seat.py` CodexSeats.test_driver_mail_to_a_busy_seat_joins_its_turn |
| 159 | `test_seat.py` CodexSeats.test_a_deadline_closes_politely_and_leaves_a_resumable_checkpoint<br>`test_seat.py` ClaudeSeats.test_a_claude_seat_runs_lean_and_streams_both_ways<br>`test_seat.py` ClaudeSeats.test_a_seat_keeps_one_process_and_resumes_after_a_stop<br>`test_seat.py` AcpSeats.test_a_grok_seat_runs_four_turns_on_one_agent |
| 49, 49b, 49e | `test_seat.py` ClaudeSeats.test_a_claude_seat_runs_lean_and_streams_both_ways |
| 154 | `test_seat.py` ClaudeSeats.test_a_claude_seat_runs_lean_and_streams_both_ways<br>`test_seat.py` ClaudeSeats.test_a_seat_keeps_one_process_and_resumes_after_a_stop |
| 49c | `test_seat.py` ClaudeSeats.test_a_seat_that_died_mid_turn_still_has_its_session |
| 49d | `test_seat.py` ClaudeSeats.test_a_recorded_claude_stream |
| 76 | `test_seat.py` ClaudeSeats.test_a_process_the_seat_forgot_dies_with_it |
| 77 | `test_seat.py` ClaudeSeats.test_a_cli_killed_from_outside_still_yields_its_session |
| 81, 82 | `test_seat.py` ClaudeSeats.test_a_seat_keeps_one_process_and_resumes_after_a_stop |
| 98 | `test_seat.py` ClaudeSeats.test_a_turn_on_another_session_is_an_error |
| 84, 155 | `test_seat.py` ClaudeSeats.test_a_recorded_three_turn_seat |
| 88 | `test_seat.py` ClaudeSeats.test_failures_are_typed_from_provider_fields_and_stderr |
| 130 | `test_seat.py` ClaudeSeats.test_a_line_separator_inside_a_turn_end_is_no_line_break<br>`test_seat.py` Pushes.test_outbox_lines_post_mail_pushes |
| 209 | `test_seat.py` ClaudeSeats.test_a_stops_rc_is_for_the_turns_it_cut |
| 210 | `test_seat.py` ClaudeSeats.test_a_relaunch_runs_the_cli_the_seat_recorded |
| 138 | `test_seat.py` ClaudeSeats.test_a_send_killed_mid_write_never_fuses_with_the_next |
| 139 | `test_seat.py` ClaudeSeats.test_two_sends_racing_to_a_stopped_seat_relaunch_it_once |
| 142, 143 | `test_seat.py` ClaudeSeats.test_driver_mail_reaches_live_and_stopped_seats_once |
| 207 | `test_seat.py` ClaudeSeats.test_the_wrapper_never_waits_for_the_delivery_lock |
| 93 | `test_seat.py` ClaudeSeats.test_a_stop_mid_turn_reaps_the_seat |
| 96 | `test_seat.py` ClaudeSeats.test_a_cli_deaf_to_eof_and_term_is_killed |
| 71 | `test_seat.py` Reaper.test_a_timeout_kills_the_whole_process_group |
| 74 | `test_seat.py` Reaper.test_an_interrupt_reaches_the_seats_group |
| 75 | `test_seat.py` Reaper.test_an_interrupt_the_caller_ignored_never_reaches_the_seat |
| 89 | `test_seat.py` Pushes.test_a_turn_end_posts_one_push |
| 89b | `test_seat.py` Pushes.test_outbox_lines_post_mail_pushes |
| 90 | `test_seat.py` Pushes.test_no_socket_is_no_push_and_a_dead_one_is_survived |
| 132 | `test_seat.py` Pushes.test_a_push_reads_no_reply_and_never_waits_for_one<br>`test_seat.py` Pushes.test_a_turn_end_posts_one_push<br>`test_seat.py` Pushes.test_no_socket_is_no_push_and_a_dead_one_is_survived |
| 134 | `test_seat.py` Pushes.test_a_long_answer_is_clipped_in_its_push |
| 153 | `test_seat.py` Pushes.test_every_wire_captures_the_socket_and_no_cli_sees_it |
| 168 | `test_seat.py` Pushes.test_every_wire_captures_the_socket_and_no_cli_sees_it<br>`test_seat.py` Pushes.test_no_probe_cli_sees_the_socket |
| 169 | `test_seat.py` Pushes.test_a_relaunch_captures_the_socket_and_hides_it |
| 164 | `test_seat.py` Pushes.test_a_job_dirs_mail_files_are_never_opened_through_a_link |
| 200 | `test_seat.py` Pushes.test_capture_never_writes_through_a_link |
| 157 | `test_seat.py` Hermetic.test_every_captured_socket_is_one_this_suite_made<br>`test_wires.py` Translation.test_the_suite_carries_no_driver_inbox |
| 12, 13, 14 | `test_seat.py` FailureText.test_usage_limits_are_provider_quota_dead_with_their_reset |
| 15, 15b, 15c | `test_seat.py` FailureText.test_logins_are_auth_expired |
| 16 | `test_seat.py` FailureText.test_other_text_is_an_error |

## tests/test_acp.py

| Old test | Now, in `test_wires.py` |
|---|---|
| test_grok_transcript_round_trips_and_translates | AcpClient.test_a_recorded_grok_seat_round_trips |
| test_opencode_transcript_round_trips_and_translates | AcpClient.test_a_recorded_opencode_seat_round_trips |
| test_agent_requests_malformed_line_and_death | AcpClient.test_agent_requests_a_malformed_line_and_a_death_mid_turn |
| test_close_frees_a_prompt_whose_pipe_an_escaped_child_holds | AcpClient.test_close_frees_a_prompt_whose_pipe_an_escaped_child_holds |
| test_a_big_prompt_while_the_agent_floods_its_stdout | AcpClient.test_a_big_prompt_while_the_agent_floods_its_stdout |
| test_close_reads_to_eof_before_it_gives_up_on_an_answer | AcpClient.test_close_reads_to_eof_before_it_gives_up_on_an_answer |
| test_close_terms_the_process_group | AcpClient.test_close_terms_the_process_group |
| test_translate_rules | Translation.test_usage_is_normalized_per_provider<br>Translation.test_a_stop_reason_types_the_turn<br>Translation.test_updates_that_are_no_events<br>Translation.test_the_answer_is_the_last_text_after_the_last_tool<br>Translation.test_the_acp_commands |
| test_set_config_waits_for_a_late_config_update | AcpClient.test_set_option_waits_for_a_late_config_update |
| test_suite_env_carries_no_driver_inbox | Translation.test_the_suite_carries_no_driver_inbox |

## Deleted with a cut

| Commit | What was cut | Rows |
|---|---|---|
| 5b9127c | the quota check before each buy | 50, 50b, 50c, 51, 52, 53, 59, 60, 61, 63, 121 |
| 12232af | --rw and read-only mode | 39i, 69a, 69c, 69d, 69e, 78b |
| 57f2087 | --sandbox | 149, 150, 151, 152, 160, 161, 162, 163, 165, 166, 167, 174, 176, 190, 194, 195, 196, 197, 203 |
| c8e075d | seat-to-seat mail | 140, 141, 171 |
| 25cfb5b | --bg, --list, --json-schema and the CLI version floor | 19, 19b, 86, 87, 125, 126, 127, 147, 148, 198 |
| 49eeb09 | rearm.sh and the token-stale check (rearm.sh rows 01-27, quota.sh rows 05a-05f, 14, 31) | all of them |
| 0f6d2f4 | a seat.py whose sweep printed no jobs root: the root is computed in-process now | 193 |

## quota.sh rows

Moved to `tests/test_quota.py` in 57bcc45, where each test keeps its old row number
(`test_<number>_...`). Rows 10, 25, 30, 30b, 43b and 46 tested the advisory verdict and were
deleted with it there.
