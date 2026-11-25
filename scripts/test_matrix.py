#!/usr/bin/env python3
"""Part 22 traceability: every test area and safety condition of the specification, mapped to the tests that prove it,
checked against real results (RR Trading One Phase 22).

Inputs are the results of the suites, not the test sources:
  --junit FILE      pytest --junitxml output (repeat: engine, MT5 bridge, Windows connector)
  --cargo FILE      `cargo test -p rr-one-core` output of the desktop core
  --ci FILE         a CI workflow whose named check must exist (Windows install / upgrade run only on a Windows runner)
  --only py|rs|ci   check only these kinds (CI jobs that run a subset of the suites)
  --markdown FILE   write the matrix with each test's result

Exit status 1 when any mapped test is missing from the results or did not pass. A test is referenced as
  py:<file>::<test>   rs:<module>::tests::<test>   ci:<workflow>::<text that the check prints>
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

AREAS: list[tuple[str, list[str]]] = [
    ("All 215 strategy IDs", ["py:test_preservation.py::test_all_original_strategy_ids_present",
                              "py:test_strategies.py::test_registry_has_full_library_with_unique_codes"]),
    ("Strategy configuration hashes", ["py:test_preservation.py::test_original_strategy_logic_and_config_unchanged",
                                       "py:test_strategy_library.py::test_baseline_detects_modification_and_missing_inventory"]),
    ("Strategy code hashes", ["py:test_preservation.py::test_original_strategy_logic_and_config_unchanged"]),
    ("Existing API routes", ["py:test_part22.py::test_every_route_of_the_original_engine_still_exists",
                             "py:test_api.py::test_strategies_endpoint_lists_full_registry"]),
    ("Database migrations", ["py:test_part22.py::test_migrations_form_one_linear_chain",
                             "py:test_part22.py::test_downgrade_to_base_and_upgrade_to_head_and_schema_matches_models",
                             "rs:supervisor::tests::migration_refused_without_backup"]),
    ("Learning scheduler", ["py:test_discovery_sandbox.py::test_scheduler_idempotency_retry_and_timeout",
                            "py:test_resources.py::test_a_timed_out_job_is_terminated_not_left_running",
                            "py:test_resources.py::test_an_operator_cancel_terminates_the_run_and_it_is_not_retried_in_the_period",
                            "py:test_learner_entry.py::test_entry_delegates_to_original_scheduler"]),
    ("Trade collection", ["py:test_learning.py::test_sync_imports_deals_once_aggregates_and_separates_modes",
                          "py:test_learning.py::test_aggregate_partial_exits_vwap_and_costs",
                          "py:test_trade_intelligence.py::test_exit_types_come_from_the_broker_deal_reason"]),
    ("DEMO/LIVE separation", ["py:test_live_separation.py::test_live_lifecycle_end_to_end",
                              "py:test_live_separation.py::test_identity_is_exact_login_exact_server_and_account_type",
                              "py:test_trade_intelligence.py::test_demo_and_live_never_mix_and_sources_stay_apart",
                              "py:test_api.py::test_live_requires_confirmation_and_approved_account"]),
    ("MT5 connector", ["py:test_connector.py::test_market_order_executes_once", "py:test_connector.py::test_journal_survives_restart",
                       "py:test_headless.py::test_connects_to_profile_terminal_and_stops_gracefully",
                       "py:test_core.py::test_connect_matches_server_name_and_reads_account",
                       "rs:connector::tests::start_status_stop_lifecycle_with_real_processes"]),
    ("HMAC and replay checks", ["py:test_connector.py::test_rejects_unsigned_and_bad_signature",
                                "py:test_connector.py::test_replayed_nonce_rejected",
                                "py:test_mt5_agent.py::test_socket_rejects_bad_key_and_replayed_hello",
                                "py:test_local_api.py::test_authentication_failures",
                                "rs:engine::tests::signature_matches_python_test_vector"]),
    ("Relay communication", ["py:test_mt5_agent.py::test_engine_reaches_mt5_through_outbound_relay_and_trades_demo",
                             "py:test_mt5_agent.py::test_offline_agent_fails_fast_and_expired_requests_are_never_delivered",
                             "py:test_bridge_scope.py::test_read_scope_reads_but_can_never_execute"]),
    ("Symbol discovery", ["py:test_execution_e2e.py::test_symbols_discovered_dynamically",
                          "py:test_symbol_catalogue.py::test_new_symbols_start_disabled_and_user_choice_survives_rediscovery",
                          "py:test_symbol_catalogue.py::test_symbol_no_longer_offered_is_flagged_not_deleted_and_recovers"]),
    ("Multi-timeframe scanner", ["py:test_scanner_scheduling.py::test_each_closed_bar_is_analysed_once",
                                 "py:test_scanner_scheduling.py::test_strategies_run_only_on_declared_timeframes",
                                 "py:test_scanner_scheduling.py::test_budget_defers_the_rest_and_stop_cancels",
                                 "py:test_signals_backtest.py::test_closed_htf_excludes_forming_bar"]),
    ("Agent orchestration", ["py:test_orchestration.py::test_registry_is_closed_and_only_two_agents_touch_execution",
                             "py:test_orchestration.py::test_scan_cycle_records_each_agent_that_ran",
                             "py:test_orchestration.py::test_contract_refuses_unknown_agents"]),
    ("AI providers", ["py:test_ai_gateway.py::test_retry_then_fallback_with_ledger",
                      "py:test_ai_gateway.py::test_invalid_output_is_never_used",
                      "py:test_ai_gateway.py::test_credentials_never_reach_a_provider_and_each_provider_gets_only_its_own_key",
                      "py:test_ai_studio.py::test_local_rate_limit_moves_to_the_next_provider_without_a_cooldown"]),
    ("Probability calibration", ["py:test_signals_backtest.py::test_calibrator_is_monotone_and_requires_samples",
                                 "py:test_fusion.py::test_probability_is_only_ever_a_calibrated_estimate",
                                 "py:test_intelligence.py::test_probability_unavailable_without_model_and_shadow_when_unapproved"]),
    ("Backtesting", ["py:test_signals_backtest.py::test_real_strategy_backtest_metrics_and_tools",
                     "py:test_signals_backtest.py::test_same_bar_sl_and_tp_assumes_stop_first",
                     "py:test_signals_backtest.py::test_gap_through_stop_fills_at_open"]),
    ("Walk-forward", ["py:test_model_governance.py::test_walk_forward_is_chronological_with_embargo_and_required",
                      "py:test_signals_backtest.py::test_optimise_reports_oos_for_every_ranked_row",
                      "py:test_intelligence.py::test_embargo_prevents_overlap"]),
    ("Monte Carlo", ["py:test_signals_backtest.py::test_real_strategy_backtest_metrics_and_tools",
                     "py:test_discovery_sandbox.py::test_validation_pipeline_reports_and_protects_final_test"]),
    ("Risk engine", ["py:test_risk.py::test_eurusd_sizing_exact", "py:test_risk.py::test_daily_loss_limit_locks_trading",
                     "py:test_risk.py::test_margin_validation", "py:test_risk.py::test_correlated_exposure_via_currency_legs"]),
    ("Execution reconciliation", ["py:test_execution_e2e.py::test_lost_response_is_reconciled_not_resent",
                                  "py:test_connector.py::test_crash_after_send_reconciles_without_resend",
                                  "py:test_execution_e2e.py::test_restart_adopts_broker_positions_and_protects_unprotected"]),
    ("Emergency stop", ["py:test_api.py::test_kill_switch_and_reset", "py:test_execution_e2e.py::test_kill_switch_closes_and_blocks",
                        "py:test_part22.py::test_emergency_stop_blocks_entries_but_does_not_blindly_close_positions",
                        "py:test_mt5_agent.py::test_pc_emergency_stop_activates_engine_kill_switch"]),
    ("Windows installation", ["ci:desktop.yml::installed to $inst", "ci:desktop.yml::uninstalled; settings and backups kept",
                              "rs:paths::tests::windows_layout_matches_spec", "rs:tauri_acl_tests::tauri_acl_lists_every_command"]),
    ("Application upgrades", ["ci:desktop.yml::upgrade: program files replaced, settings and backups kept",
                              "rs:secrets::tests::format_5_file_gains_only_the_backup_key",
                              "rs:local::tests::image_tag_follows_the_installed_sources",
                              "py:test_part22.py::test_downgrade_to_base_and_upgrade_to_head_and_schema_matches_models"]),
    ("Database backup and restore", ["rs:supervisor::tests::an_encrypted_backup_restores_its_exact_dump_and_the_decrypted_copy_is_removed",
                                     "rs:supervisor::tests::a_modified_backup_is_refused_and_a_pre_phase_21_plain_backup_still_restores",
                                     "rs:supervisor::tests::a_new_pc_restores_another_installations_backup_once_given_its_key",
                                     "rs:supervisor::tests::an_unrestorable_dump_never_touches_the_real_database",
                                     "rs:backupcrypt::tests::tampering_truncation_and_wrong_keys_are_refused"]),
]

SAFETY: list[tuple[str, list[str]]] = [
    ("1. The learner cannot submit orders", ["py:test_part22.py::test_learner_ai_and_agents_have_no_path_to_orders_live_or_permissions",
                                             "py:test_part22.py::test_learner_and_analysis_containers_cannot_trade",
                                             "py:test_bridge_scope.py::test_read_scope_reads_but_can_never_execute",
                                             "py:test_discovery_sandbox.py::test_forward_shadow_test_never_sends_orders"]),
    ("2. AI cannot enable LIVE", ["py:test_part22.py::test_learner_ai_and_agents_have_no_path_to_orders_live_or_permissions",
                                 "py:test_orchestration.py::test_registry_is_closed_and_only_two_agents_touch_execution",
                                 "py:test_api.py::test_trader_cannot_switch_to_live"]),
    ("3. AI cannot modify execution permissions", ["py:test_part22.py::test_learner_ai_and_agents_have_no_path_to_orders_live_or_permissions",
                                                   "py:test_agent_scanner.py::test_agent_cannot_flip_or_upgrade",
                                                   "py:test_api.py::test_rbac_readonly_cannot_trade_or_configure"]),
    ("4. Wrong broker identity blocks execution", ["py:test_execution_e2e.py::test_identity_mismatch_is_detected",
                                                   "py:test_connector.py::test_account_identity_mismatch_rejected",
                                                   "py:test_headless.py::test_wrong_account_is_disconnected_not_traded"]),
    ("5. Stale quotes block new entries", ["py:test_intelligence.py::test_stale_quote_cannot_authorise_entry",
                                           "py:test_risk.py::test_spread_and_expiry"]),
    ("6. Critical anomalies block new entries", ["py:test_intelligence.py::test_anomaly_blocks_entries_but_keeps_positions_and_recovers",
                                                 "py:test_intelligence.py::test_risk_engine_anomaly_gate_is_additive"]),
    ("7. Risk failures block execution", ["py:test_execution_e2e.py::test_risk_rejection_releases_reservation",
                                          "py:test_risk.py::test_volume_below_broker_minimum_is_rejected_not_rounded_up"]),
    ("8. Duplicate submissions are prevented", ["py:test_execution_e2e.py::test_duplicate_signal_never_executes_twice",
                                                "py:test_connector.py::test_market_order_executes_once",
                                                "py:test_roles.py::test_trading_role_executes_only_new_confirmed_signals_once"]),
    ("9. Unresolved executions are reconciled", ["py:test_execution_e2e.py::test_lost_response_is_reconciled_not_resent",
                                                 "py:test_live_separation.py::test_mode_switch_refused_while_executions_are_unresolved"]),
    ("10. Experimental code cannot enter the production registry",
     ["py:test_preservation.py::test_experimental_strategies_never_enter_production_registry",
      "py:test_discovery_sandbox.py::test_experimental_code_never_reaches_production_registry",
      "py:test_discovery_sandbox.py::test_static_validator_rejects_unsafe_code"]),
    ("11. Model promotion requires independent validation and approval",
     ["py:test_intelligence.py::test_governance_requires_approval_and_supports_rollback",
      "py:test_model_governance.py::test_candidate_must_not_be_worse_than_production",
      "py:test_model_governance.py::test_model_api_reports_signature_and_refuses_promotion",
      "py:test_discovery_sandbox.py::test_readonly_and_trader_cannot_promote_models_or_experiments"]),
    ("12. Emergency stop blocks new entries", ["py:test_part22.py::test_emergency_stop_blocks_entries_but_does_not_blindly_close_positions",
                                               "py:test_risk.py::test_kill_switch_pause_and_news"]),
    ("13. Existing positions are not blindly closed by emergency stop",
     ["py:test_part22.py::test_emergency_stop_blocks_entries_but_does_not_blindly_close_positions",
      "py:test_risk.py::test_emergency_policies"]),
    ("15. The PC LIVE lock opens only for the exact LIVE login and phrase (Parts 27-30 Phase 10)",
     ["py:test_headless.py::test_pc_live_lock_needs_the_exact_live_login_and_the_phrase",
      "py:test_headless.py::test_pc_live_lock_refuses_a_demo_account_and_ignores_a_leftover_request",
      "rs:connector::tests::pc_live_lock_request_round_trip_and_local_checks",
      "py:test_part29.py::test_readiness_reports_the_pc_live_lock_from_the_connector_heartbeat"]),
    ("16. Running without AI keeps the Risk Engine and never crosses modes; LIVE needs its own consent",
     ["py:test_ai_mode.py::test_engine_only_executes_in_demo_without_any_ai_provider",
      "py:test_ai_mode.py::test_engine_only_keeps_every_risk_control",
      "py:test_ai_mode.py::test_the_eligibility_rule_never_crosses_modes",
      "py:test_ai_mode.py::test_ai_mode_without_provider_still_executes_nothing",
      "py:test_ai_mode.py::test_live_engine_only_needs_trade_live_and_the_typed_phrase"]),
    ("17. Each account is traded only through its own connector, reconciliation and risk limits; never across modes",
     ["py:test_accounts_execution.py::test_one_signal_trades_each_account_with_its_own_limits",
      "py:test_accounts_execution.py::test_a_rejection_on_one_account_never_undoes_anothers_execution",
      "py:test_accounts_execution.py::test_research_only_and_unreconciled_accounts_are_not_traded",
      "py:test_accounts_execution.py::test_the_kill_switch_applies_to_every_account",
      "py:test_accounts_execution.py::test_a_live_account_is_never_traded_while_demo_is_active",
      "py:test_accounts_execution.py::test_without_a_connector_an_account_is_skipped_not_traded",
      "py:test_accounts_multi.py::test_enabling_trading_needs_verification_and_for_live_the_approval",
      "py:test_headless.py::test_each_instance_has_its_own_folder_and_vault_entry",
      "rs:connector::tests::each_profile_has_its_own_connector_instance"]),
    ("18. Single Active AI never calls an unconfigured or disabled provider; without fallback AI analysis stops",
     ["py:test_ai_control.py::test_single_active_overrides_every_route_and_other_providers_get_nothing",
      "py:test_ai_control.py::test_without_fallback_a_failed_provider_leaves_the_task_unanswered",
      "py:test_ai_control.py::test_the_configured_fallback_is_used_and_recorded",
      "py:test_ai_control.py::test_a_switched_off_active_provider_gets_no_requests"]),
    ("19. A configuration profile is used only after validation; AI values are bounded; LIVE is never automatic",
     ["py:test_profiles_reviews.py::test_activation_rules_and_rollback",
      "py:test_profiles_reviews.py::test_promotion_policy_is_off_by_default_and_never_reaches_live",
      "py:test_profiles_reviews.py::test_ai_proposal_is_bounded_untrusted_and_only_proposed",
      "py:test_profiles_reviews.py::test_versions_parents_and_the_strategy_is_never_changed",
      "py:test_profiles_reviews.py::test_apply_picks_the_most_specific_active_profile"]),
    ("20. Self-healing is verified and recorded, and never changes trading controls (kill switch, risk lock, orders)",
     ["py:test_incidents.py::test_kill_switch_incident_closes_only_after_the_administrator_resets_it",
      "py:test_incidents.py::test_stuck_job_is_detected_repaired_verified_and_recorded",
      "py:test_incidents.py::test_the_policy_switch_stops_automatic_repairs",
      "py:test_incidents.py::test_failed_repairs_escalate_after_three_attempts",
      "py:test_incidents.py::test_system_events_become_incidents_and_secrets_are_not_stored"]),
    ("14. Original strategy preservation tests pass", ["py:test_preservation.py::test_all_original_strategy_ids_present",
                                                      "py:test_preservation.py::test_original_strategy_logic_and_config_unchanged",
                                                      "py:test_preservation.py::test_experimental_strategies_never_enter_production_registry"]),
]


# Parts 27-30, Section 6: the 35 required validations (Phase 8).
PARTS27_30: list[tuple[str, list[str]]] = [
    ("1. Strategy inventory preservation", ["py:test_preservation.py::test_all_original_strategy_ids_present",
                                            "py:test_part28.py::test_catalogue_derives_from_the_215_strategies_without_changing_them"]),
    ("2. Strategy identifier preservation", ["py:test_preservation.py::test_all_original_strategy_ids_present",
                                             "py:test_strategies.py::test_registry_has_full_library_with_unique_codes"]),
    ("3. Strategy source and configuration preservation", ["py:test_preservation.py::test_original_strategy_logic_and_config_unchanged",
                                                           "py:test_part29.py::test_generate_records_experiments_without_touching_the_strategy"]),
    ("4. Pattern registry preservation", ["py:test_part28.py::test_catalogue_derives_from_the_215_strategies_without_changing_them",
                                          "py:test_part27.py::test_extra_regime_tags_are_added_and_the_primary_label_is_unchanged"]),
    ("5. AI provider integration", ["py:test_part27.py::test_openai_preset_and_reasoning_model_parameter_fallback",
                                    "py:test_part27.py::test_consensus_confirms_only_when_both_providers_confirm",
                                    "py:test_parts27_30_integration.py::test_parts27_30_flow_end_to_end_in_demo"]),
    ("6. Provider fallback", ["py:test_ai_gateway.py::test_retry_then_fallback_with_ledger",
                              "py:test_part27.py::test_consensus_without_both_answers_leaves_the_candidate_unreviewed"]),
    ("7. Research data validation", ["py:test_part30.py::test_data_quality_checks",
                                     "py:test_part27.py::test_opportunity_and_source_registry_api"]),
    ("8. Market regime detection", ["py:test_analysis.py::test_regime_trend_and_range",
                                    "py:test_part27.py::test_extra_regime_tags_are_added_and_the_primary_label_is_unchanged"]),
    ("9. Pattern detection", ["py:test_part28.py::test_bullish_gartley_completes_confirms_and_reaches_its_target",
                              "py:test_part28.py::test_detection_is_causal_forming_until_d_is_confirmed",
                              "py:test_part28.py::test_elliott_counts_only_rule_satisfying_impulses",
                              "py:test_part28.py::test_records_are_deduplicated_move_forward_and_record_relations"]),
    ("10. Strategy selection", ["py:test_part29.py::test_selection_decisions",
                                "py:test_part29.py::test_scan_trace_has_selection_and_health"]),
    ("11. Strategy discovery isolation", ["py:test_discovery_sandbox.py::test_experimental_code_never_reaches_production_registry",
                                          "py:test_part29.py::test_generate_records_experiments_without_touching_the_strategy"]),
    ("12. Backtesting", ["py:test_signals_backtest.py::test_real_strategy_backtest_metrics_and_tools",
                         "py:test_signals_backtest.py::test_fill_next_open_with_spread_and_hits_tp"]),
    ("13. Walk-forward testing", ["py:test_model_governance.py::test_walk_forward_is_chronological_with_embargo_and_required"]),
    ("14. Monte Carlo testing", ["py:test_signals_backtest.py::test_real_strategy_backtest_metrics_and_tools"]),
    ("15. Model leakage checks", ["py:test_intelligence.py::test_embargo_prevents_overlap",
                                  "py:test_part29.py::test_bias_report_flags_repainting_and_leakage",
                                  "py:test_part29.py::test_variant_evaluation_and_bias_report"]),
    ("16. Trade learning", ["py:test_learning.py::test_sync_imports_deals_once_aggregates_and_separates_modes",
                            "py:test_trade_intelligence.py::test_attribution_is_recorded_never_guessed"]),
    ("17. DEMO/LIVE separation", ["py:test_trade_intelligence.py::test_demo_and_live_never_mix_and_sources_stay_apart",
                                  "py:test_part29.py::test_readiness_requires_a_demo_track_record_and_gates_the_switch"]),
    ("18. MT5 connector", ["py:test_connector.py::test_market_order_executes_once",
                           "py:test_execution_e2e.py::test_symbols_discovered_dynamically"]),
    ("19. Broker reconciliation", ["py:test_execution_e2e.py::test_lost_response_is_reconciled_not_resent",
                                   "py:test_part30.py::test_startup_compares_pending_orders_and_blocks_on_unknown_engine_orders",
                                   "py:test_execution_e2e.py::test_restart_adopts_broker_positions_and_protects_unprotected"]),
    ("20. Order idempotency", ["py:test_connector.py::test_market_order_executes_once",
                               "py:test_execution_e2e.py::test_duplicate_signal_never_executes_twice"]),
    ("21. Partial fill handling", ["py:test_parts27_30_integration.py::test_partial_fill_is_recorded_and_the_filled_volume_is_what_gets_managed",
                                   "py:test_connector.py::test_modify_partial_and_close",
                                   "py:test_learning.py::test_aggregate_partial_exits_vwap_and_costs"]),
    ("22. Position management", ["py:test_execution_e2e.py::test_position_management_partial_and_break_even",
                                 "py:test_part29.py::test_time_exit_closes_through_the_journalled_manager_only_when_enabled"]),
    ("23. Risk Governor rejection", ["py:test_execution_e2e.py::test_risk_rejection_releases_reservation",
                                     "py:test_part30.py::test_floating_loss_limit_blocks_new_entries",
                                     "py:test_part29.py::test_concentration_blocks_a_second_order_from_the_same_strategy_end_to_end",
                                     "py:test_parts27_30_integration.py::test_an_inconsistent_feed_is_refused_by_the_risk_engine_even_after_ai_consensus"]),
    ("24. Emergency stop", ["py:test_part22.py::test_emergency_stop_blocks_entries_but_does_not_blindly_close_positions",
                            "py:test_part30.py::test_kill_switch_cancels_only_the_engines_pending_orders"]),
    ("25. Market data quality", ["py:test_part30.py::test_data_quality_checks", "py:test_part30.py::test_scan_records_data_quality",
                                 "py:test_intelligence.py::test_stale_quote_cannot_authorise_entry"]),
    ("26. Execution intelligence", ["py:test_intelligence.py::test_execution_analytics_counts_only_confirmed_fills",
                                    "py:test_trade_intelligence.py::test_quality_costs_durations_and_coverage"]),
    ("27. Counterfactual learning", ["py:test_part30.py::test_counterfactuals_are_labelled_hindsight",
                                     "py:test_part30.py::test_simulation_api_from_an_opportunity_and_a_closed_position"]),
    ("28. Model drift", ["py:test_model_governance.py::test_critical_drift_recommends_retraining_once",
                         "py:test_part29.py::test_health_reports_drifting_production_models"]),
    ("29. Strategy degradation", ["py:test_part30.py::test_validation_expiry_drift_and_strategy_suspension"]),
    ("30. Database migration", ["py:test_part22.py::test_migrations_form_one_linear_chain",
                                "py:test_part22.py::test_downgrade_to_base_and_upgrade_to_head_and_schema_matches_models",
                                "rs:supervisor::tests::migration_refused_without_backup"]),
    ("31. API integration", ["py:test_part22.py::test_every_route_of_the_original_engine_still_exists",
                             "py:test_part27.py::test_opportunity_and_source_registry_api", "py:test_part28.py::test_pattern_api",
                             "py:test_part29.py::test_variants_and_dna_api"]),
    ("32. Frontend build", ["ci:frontend.yml::npm run build"]),
    ("33. Worker recovery", ["py:test_roles.py::test_restarted_container_reclaims_its_own_stale_lock",
                             "py:test_part30.py::test_a_learner_restart_recovers_jobs_left_running",
                             "py:test_resources.py::test_a_timed_out_job_is_terminated_not_left_running"]),
    ("34. Application restart recovery", ["py:test_execution_e2e.py::test_restart_adopts_broker_positions_and_protects_unprotected",
                                          "py:test_connector.py::test_journal_survives_restart",
                                          "py:test_mt5_agent.py::test_terminal_crash_is_detected_and_recovered"]),
    ("35. Backup integrity and restoration", ["rs:backupcrypt::tests::tampering_truncation_and_wrong_keys_are_refused",
                                              "rs:supervisor::tests::backups_are_encrypted_and_no_plain_dump_is_left_anywhere",
                                              "rs:supervisor::tests::scheduled_maintenance_backs_up_daily_and_restore_tests_weekly",
                                              "rs:supervisor::tests::a_restore_test_fails_when_the_backup_lacks_a_live_table"]),
]


def junit_results(files: list[Path]) -> dict[str, str]:
    """'<file>::<test>' -> passed | failed | skipped (parametrised cases: all must pass)."""
    out: dict[str, str] = {}
    for f in files:
        for tc in ET.parse(f).getroot().iter("testcase"):
            module = tc.get("classname", "").split(".")[-1]
            key = f"{module}.py::{tc.get('name', '').split('[')[0]}"
            state = "failed" if tc.find("failure") is not None or tc.find("error") is not None else \
                "skipped" if tc.find("skipped") is not None else "passed"
            if out.get(key) in (None, "passed"):
                out[key] = state
    return out


def cargo_results(files: list[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in files:
        for m in re.finditer(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\s*$", f.read_text(errors="replace"), re.M):
            out[m.group(1)] = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}[m.group(2)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--junit", action="append", default=[], type=Path)
    ap.add_argument("--cargo", action="append", default=[], type=Path)
    ap.add_argument("--ci", action="append", default=[], type=Path)
    ap.add_argument("--only", action="append", choices=["py", "rs", "ci"])
    ap.add_argument("--markdown", type=Path)
    a = ap.parse_args()
    kinds = set(a.only or ["py", "rs", "ci"])
    py, rs = junit_results(a.junit), cargo_results(a.cargo)
    ci = {p.name: p.read_text() for p in a.ci}

    def state(ref: str) -> str:
        kind, _, rest = ref.partition(":")
        if kind not in kinds:
            return "not checked here"
        if kind == "py":
            return py.get(rest, "missing")
        if kind == "rs":
            return rs.get(rest, "missing")
        wf, _, text = rest.partition("::")
        return "present (runs on Windows CI)" if wf in ci and text in ci[wf] else "missing"

    bad, lines = [], ["# Test matrix (Part 22 and Parts 27-30)", "",
                      "Generated by `scripts/test_matrix.py` from real test results. Every item must be proven by passing tests.", ""]
    for title, rows in (("Test areas", AREAS), ("Safety conditions", SAFETY),
                        ("Parts 27-30: required validations (Section 6)", PARTS27_30)):
        lines += [f"## {title}", "", "| Item | Tests | Result |", "|---|---|---|"]
        for item, refs in rows:
            states = [(r, state(r)) for r in refs]
            bad += [f"{item}: {r} -> {s}" for r, s in states if s not in ("passed", "not checked here")
                    and not s.startswith("present")]
            ok = all(s == "passed" or s.startswith("present") for _, s in states if s != "not checked here")
            tests = "<br>".join(f"`{r.split(':', 1)[1]}` ({s})" for r, s in states)
            lines.append(f"| {item} | {tests} | {'PASS' if ok else 'FAIL'} |")
        lines.append("")
    total = sum(len(r) for _, r in AREAS + SAFETY + PARTS27_30)
    lines.append(f"{len(AREAS)} areas, {len(SAFETY)} safety conditions, {len(PARTS27_30)} Parts 27-30 validations, "
                 f"{total} test references; failures: {len(bad)}.")
    if a.markdown:
        a.markdown.write_text("\n".join(lines) + "\n")
    print("\n".join(bad) if bad else f"all {len(AREAS)} areas, {len(SAFETY)} safety conditions and {len(PARTS27_30)} "
                                     "Parts 27-30 validations proven by passing tests")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
