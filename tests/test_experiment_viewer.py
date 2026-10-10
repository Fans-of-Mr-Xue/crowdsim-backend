"""Offline viewer evidence checks; generated fixtures never start SUMO."""

import csv
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from crowdsim.infrastructure.experiment_viewer import (
    METRICS, DEFAULT_SCOPE_REFERENCES, build_viewer, load_run, number,
    scope_version, _sample_indices, _load_scope_references,
)


class ExperimentViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "runs/run-test"
        self.run.mkdir(parents=True)
        self.write_json("manifest.json", {
            "run_id": "run-test", "scenario": {"location_id": "east-nanjing-road"},
            "requirement": {"requirement_id": "req-test", "spatial_scope": {"name": "测试区域"}},
            "demand": {"planned": 10},
        })
        polygon = lambda x: {"type": "Polygon", "coordinates": [[[x,0],[x+10,0],[x+10,10],[x,10],[x,0]]]}
        self.write_json("observation_geometry.json", {
            "schema_version": 1, "area_m2": 200, "requested_metric_ids": [m[1] for m in METRICS],
            "parameters": {"grid_size_m": 10},
            "scope_geometry": {"type":"Polygon", "coordinates":[[[0,0],[20,0],[20,10],[0,10],[0,0]]]},
            "cells": [{"cell_id":"a","area_m2":100,"geometry":polygon(0)},
                      {"cell_id":"b","area_m2":100,"geometry":polygon(10)}],
            "boundaries": [{"boundary_id":"ab","cell_a":"a","cell_b":"b","direction_from_a":"east",
                            "geometry":{"type":"LineString","coordinates":[[10,0],[10,10]]}},
                           {"boundary_id":"outside","cell_a":"a","cell_b":None,"direction_from_a":"west",
                            "geometry":{"type":"LineString","coordinates":[[0,0],[0,10]]}}],
        })
        self.write_json("observation_result.json", {"status":"interrupted", "schema_version":1,
                        "metrics": {m[1]:{"status":"recorded"} for m in METRICS}})
        self.transitions = [
            {"snapshot_id":"s0","time_seconds":.5,"person_id":"p1","dimension":"behavior_state","from_state":None,"to_state":"walking"},
            {"snapshot_id":"s0","time_seconds":.5,"person_id":"p1","dimension":"presence","from_state":"outside","to_state":"inside"},
            {"snapshot_id":"s1","time_seconds":1.,"person_id":"p1","dimension":"behavior_state","from_state":"walking","to_state":"waiting"},
            {"snapshot_id":"s1","time_seconds":1.,"person_id":"p1","dimension":"psychological_state","from_state":"calm","to_state":"tense"},
            {"snapshot_id":"s1","time_seconds":1.,"person_id":"p2","dimension":"presence","from_state":"inside","to_state":"outside"},
        ]
        self.indices = [{"snapshot_id":f"s{i}", "time_seconds":t, "cell_rows":2,"boundary_rows":2,
                         "transition_rows": [2,3,0][i]} for i,t in enumerate([.5,1.,2.])]
        self.global_rows = []
        self.cell_rows = []
        self.boundary_rows = []
        for i,(time,counts) in enumerate([(.5,[2,6]),(1.,[3,3]),(2.,[0,0])]):
            sid=f"s{i}"
            self.global_rows.append({"snapshot_id":sid,"time_seconds":time,"person_count":sum(counts),
                "network_person_count":10 if i<2 else 0,"outside_scope_person_count":10-sum(counts) if i<2 else 0,
                "invalid_position_count":0,"density_person_per_m2":sum(counts)/200,"area_m2":200,
                "speed_sample_count":sum(counts),"avg_speed_mps":1 if i==0 else 0 if i==1 else ""})
            for cid,count in zip(["a","b"],counts):
                self.cell_rows.append({"snapshot_id":sid,"time_seconds":time,"cell_id":cid,
                                      "person_count":count,"density_person_per_m2":count/100,
                                      "avg_speed_mps":1 if i==0 else 0 if i==1 else ""})
            for bid in ["ab","outside"]:
                self.boundary_rows.append({"snapshot_id":sid,"time_seconds":time,"boundary_id":bid,
                    "difference_person_per_m2":(counts[0]-counts[1])/100 if bid=="ab" else "",
                    "status":"valid" if bid=="ab" else "outside_not_observed"})
        self.flush()

    def write_json(self,name,value):
        (self.run/name).write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")

    def write_jsonl(self,name,rows):
        (self.run/name).write_text("".join(json.dumps(row)+"\n" for row in rows),encoding="utf-8")

    def write_csv(self,name,rows):
        with (self.run/name).open("w",newline="",encoding="utf-8") as stream:
            writer=csv.DictWriter(stream,fieldnames=rows[0].keys());writer.writeheader();writer.writerows(rows)

    def flush(self):
        self.write_jsonl("observation_samples.jsonl",self.indices)
        self.write_jsonl("state_transitions.jsonl",self.transitions)
        self.write_csv("observation_global.csv",self.global_rows)
        self.write_csv("observation_cells.csv",self.cell_rows)
        self.write_csv("observation_boundaries.csv",self.boundary_rows)

    def new_state(self):
        geometry=json.loads((self.run/"observation_geometry.json").read_text());geometry["schema_version"]=2
        self.write_json("observation_geometry.json",geometry)
        state={"status":"complete", "completion_evidence_committed":True,
               "event_start_time_seconds":.5,"strategy_applied_time_seconds":1.,"completion_time_seconds":2.,
               "initial_density":{"snapshot_id":"s0","valid":True},
               "strategy_density":{"snapshot_id":"s1","valid":True},
               "final_density":{"snapshot_id":"s2","valid":True},
               "progress":{"target_person_count":10,"normally_arrived_person_count":10,"remaining_person_count":0,
                           "active_person_count":0,"pending_person_count":0,"explicitly_removed_person_count":0,
                           "unknown_disappearance_count":0,"unexpected_person_count":0,"conservation_error":0},
               "metrics":{"evacuation-time":{"status":"complete"},"evacuation-efficiency":{"status":"complete"}}}
        self.write_json("evacuation_state.json",state)
        return state

    def scope_fixture(self):
        geometry=json.loads((self.run/"observation_geometry.json").read_text())
        geometry["source_boundary"]=geometry["scope_geometry"]
        geometry["projection"]={"proj_parameter":"test-projection", "net_offset_xy":[0,0]}
        geometry["network_sha256"]="test-network"
        self.write_json("observation_geometry.json",geometry)
        reference={"name":"测试预设", "revision":"test", "boundary":geometry["source_boundary"],
                   "scope_geometry":geometry["scope_geometry"], "area_m2":200,
                   "projection":geometry["projection"], "network_sha256":"test-network"}
        return geometry,json.loads(json.dumps(reference))

    def test_scope_version_ignores_closing_point_start_and_winding(self):
        ring=[[0,0],[20,0],[20,10],[0,10]]
        expected=scope_version({"type":"Polygon","coordinates":[ring]})
        for equivalent in [ring+[ring[0]],ring[2:]+ring[:2],list(reversed(ring))]:
            self.assertEqual(scope_version({"type":"Polygon","coordinates":[equivalent]}),expected)
        self.assertIsNone(scope_version({"type":"Polygon","coordinates":[[1,2,3]]}))

    def test_reference_never_changes_historical_metrics_or_source_files(self):
        geometry,reference=self.scope_fixture()
        self.new_state()
        before=load_run(self.run)
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.run.iterdir()}
        reference={**reference,"boundary":{"type":"Polygon","coordinates":[[[0,0],[30,0],[30,10],[0,10],[0,0]]]},
                   "scope_geometry":{"type":"Polygon","coordinates":[[[0,0],[30,0],[30,10],[0,10],[0,0]]]},"area_m2":300}
        after=load_run(self.run,scope_references={"east-nanjing-road":reference})
        for key in ["area","geometry","samples","maps","ranges","metrics","evacuation",
                    "density_difference_means","transitions","transition_matrices","quality"]:
            self.assertEqual(before[key],after[key],key)
        self.assertEqual(after["scope_info"]["status"],"changed")
        self.assertEqual(after["scope_info"]["area_delta_m2"],100)
        self.assertEqual(after["scope_info"]["overlay_geometry"],reference["scope_geometry"])
        self.assertEqual(hashes,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.run.iterdir()})

    def test_same_area_does_not_mean_same_boundary(self):
        geometry,reference=self.scope_fixture()
        reference={**reference,"boundary":{"type":"Polygon","coordinates":[[[1,0],[21,0],[21,10],[1,10],[1,0]]]}}
        run=load_run(self.run,scope_references={"east-nanjing-road":reference})
        self.assertEqual(run["scope_info"]["status"],"changed")
        self.assertEqual(run["scope_info"]["area_delta_m2"],0)

    def test_reference_overlay_translates_offsets_and_rejects_incompatible_projection(self):
        geometry,reference=self.scope_fixture()
        geometry["projection"]["net_offset_xy"]=[50,10]
        self.write_json("observation_geometry.json",geometry)
        run=load_run(self.run,scope_references={"east-nanjing-road":reference})
        self.assertEqual(run["scope_info"]["overlay_geometry"]["coordinates"][0][0],[50,10])
        reference["projection"]["proj_parameter"]="different-projection"
        run=load_run(self.run,scope_references={"east-nanjing-road":reference})
        self.assertIsNone(run["scope_info"]["overlay_geometry"])

    def test_missing_original_boundary_does_not_claim_preset_match(self):
        geometry,reference=self.scope_fixture()
        geometry.pop("source_boundary")
        self.write_json("observation_geometry.json",geometry)
        run=load_run(self.run,scope_references={"east-nanjing-road":reference})
        self.assertEqual(run["scope_info"]["status"],"unavailable")
        self.assertTrue(run["scope_info"]["version"].startswith("xy-"))

    def test_new_matching_scope_is_preferred_over_longer_historical_run(self):
        geometry,reference=self.scope_fixture()
        new_run=self.root/"runs/run-new"
        shutil.copytree(self.run,new_run)
        manifest=json.loads((new_run/"manifest.json").read_text());manifest["run_id"]=new_run.name
        (new_run/"manifest.json").write_text(json.dumps(manifest))
        # An original boundary shift of one metre keeps the old area but is a distinct scope.
        old_geometry=json.loads(json.dumps(geometry))
        for point in old_geometry["source_boundary"]["coordinates"][0]:point[0]-=1
        self.write_json("observation_geometry.json",old_geometry)
        (new_run/"observation_samples.jsonl").write_text(json.dumps(self.indices[0])+"\n")
        config=self.root/"references.json"
        config.write_text(json.dumps({"references":{"east-nanjing-road":reference}}))
        data=build_viewer(self.root/"runs",self.root/"viewer",scope_reference_file=config)
        self.assertEqual([run["id"] for run in data["runs"]],["run-new","run-test"])
        self.assertEqual(data["runs"][0]["scope_info"]["status"],"matching")

    def test_invalid_explicit_reference_file_fails_without_export(self):
        config=self.root/"references.json"
        config.write_text(json.dumps({"references":{"east-nanjing-road":{"boundary":None,"area_m2":300}}}))
        output=self.root/"viewer"
        with self.assertRaises(ValueError):build_viewer(self.root/"runs",output,scope_reference_file=config)
        self.assertFalse(output.exists())

    def test_new_bund_scope_recording_and_viewer_use_actual_117_cell_grid(self):
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        from crowdsim.infrastructure.observation_metrics import ObservationCollector
        from crowdsim.infrastructure.observation_recorder import ObservationRecorder
        from tests.test_observation_metrics import person, snapshot, measure

        reference=_load_scope_references(DEFAULT_SCOPE_REFERENCES)["east-nanjing-road"]
        network=ResearchNetwork(str(Path(__file__).resolve().parents[1]/"scenarios/east_nanjing_road/east_nanjing.net.xml"))
        requirement={"spatial_scope":{"boundary":reference["boundary"]},
                     "observation":{"metric_ids":[m[1] for m in METRICS]}}
        collector=ObservationCollector.from_requirement({"requirement":requirement},network)
        self.assertEqual(len(collector.cells),117)
        self.assertEqual(len(collector.boundaries),255)
        self.assertAlmostEqual(collector.scope.area,reference["area_m2"],places=5)
        for p in self.run.iterdir():
            if p.name!="manifest.json":p.unlink()
        recorder=ObservationRecorder(self.run,collector)
        inside=collector.scope.representative_point()
        sample=measure(collector,snapshot(.5,person("a",inside.x,inside.y)))
        recorder.record(sample);recorder.finalize("interrupted","test_without_sumo")
        run=load_run(self.run,scope_references={"east-nanjing-road":reference})
        self.assertEqual(run["quality"],[])
        self.assertEqual(run["scope_info"]["status"],"matching")
        self.assertEqual(run["scope_info"]["vertex_count"],16)
        self.assertEqual(len(run["maps"]["density"][0]),117)
        self.assertEqual(len(run["maps"]["boundary"][0]),255)
        self.assertAlmostEqual(run["samples"][0]["density_person_per_m2"],1/reference["area_m2"])
        self.assertAlmostEqual(sum(cell["area"] for cell in run["geometry"]["cells"]),run["area"])

    def test_missing_values_are_not_zero(self):
        run=load_run(self.run)
        self.assertEqual(run["maps"]["speed"][1],[0,0])
        self.assertEqual(run["maps"]["speed"][2],[None,None])
        self.assertEqual(run["maps"]["density"][2],[0,0])
        self.assertIsNone(run["maps"]["boundary"][0][1])
        self.assertEqual(run["maps"]["boundary"][0][0],-.04)

    def test_index_filters_partial_appends_and_deduplicates(self):
        self.cell_rows += [dict(self.cell_rows[0]),{**self.cell_rows[0],"snapshot_id":"uncommitted","density_person_per_m2":999}]
        self.global_rows += [{**self.global_rows[0],"snapshot_id":"uncommitted","time_seconds":999}]
        self.flush()
        with (self.run/"observation_samples.jsonl").open("a") as stream:stream.write('{"snapshot_id":')
        run=load_run(self.run)
        self.assertEqual(len(run["samples"]),3)
        self.assertAlmostEqual(run["ranges"]["density"],.06)
        self.assertTrue(any("未提交" in q["message"] for q in run["quality"]))
        self.assertTrue(any("重复行" in q["message"] for q in run["quality"]))

    def test_time_mismatch_does_not_enter_maps(self):
        self.cell_rows[0]["time_seconds"]=123
        self.flush();run=load_run(self.run)
        self.assertIsNone(run["maps"]["density"][0][0])
        self.assertTrue(run["quality"])

    def test_initial_labels_and_membership_are_not_conversions(self):
        run=load_run(self.run)
        self.assertNotIn("behavior_state",run["transitions"][0])
        self.assertNotIn("scope_entry",run["transitions"][0])
        self.assertEqual(run["transitions"][1]["behavior_state"],1)
        self.assertEqual(run["transitions"][1]["scope_exit"],1)
        self.assertEqual(run["transition_matrices"]["behavior_state"],[["walking","waiting",1]])

    def test_incomplete_transition_frame_has_no_counts_or_matrix(self):
        self.transitions.pop();self.flush()
        run=load_run(self.run)
        self.assertIsNone(run["transitions"][1])
        self.assertEqual(run["transition_matrices"]["behavior_state"],[])

    def test_legacy_uses_first_start_not_resume_for_partial_b3(self):
        self.write_jsonl("commands.jsonl",[{"result":{"action":"start","status":"applied","applied_at":t}} for t in [.5,1.]])
        run=load_run(self.run)
        self.assertEqual(run["evacuation"]["t0"],.5)
        self.assertEqual(run["metrics"]["absolute-evacuation-density"]["status"],"partial_reconstructed")
        self.assertAlmostEqual(run["maps"]["initialRuntime"][1][0],.01)
        self.assertAlmostEqual(run["maps"]["initialRuntime"][1][1],.03)
        self.assertAlmostEqual(run["density_difference_means"][1][0],.02)
        self.assertEqual(run["maps"]["runtimeFinal"][2],[None,None])
        self.assertIsNone(run["evacuation"]["duration"])

    def test_no_start_is_not_an_event_baseline(self):
        run=load_run(self.run)
        self.assertIsNone(run["evacuation"]["t0"])
        self.assertEqual(run["maps"]["initialRuntime"][0],[None,None])

    def test_v2_normal_completion_formula_and_three_distributions(self):
        self.new_state();run=load_run(self.run)
        self.assertEqual(run["evacuation"]["duration"],1.)
        self.assertAlmostEqual(run["evacuation"]["efficiency"],.03)
        self.assertEqual(run["maps"]["runtimeFinal"][1],[.03,.03])
        self.assertEqual(run["maps"]["initialFinal"][1],[.02,.06])
        self.assertEqual(run["metrics"]["absolute-evacuation-density"]["status"],"complete")

    def test_uncommitted_or_abnormal_completion_is_not_final(self):
        state=self.new_state()
        for mutate in [lambda s:s.update(completion_evidence_committed=False),
                       lambda s:s["final_density"].update(snapshot_id="absent"),
                       lambda s:s["progress"].update(explicitly_removed_person_count=1),
                       lambda s:s["progress"].update(normally_arrived_person_count=9)]:
            altered=json.loads(json.dumps(state));mutate(altered);self.write_json("evacuation_state.json",altered)
            run=load_run(self.run)
            self.assertIsNone(run["evacuation"]["te"])
            self.assertIsNone(run["evacuation"]["duration"])
            self.assertEqual(run["maps"]["initialFinal"][2],[None,None])

    def test_completed_record_is_not_automatically_evacuated(self):
        self.write_json("observation_result.json",{"status":"complete"})
        run=load_run(self.run)
        self.assertIsNone(run["evacuation"]["te"])

    def test_bad_position_invalidates_b3_not_zeroes(self):
        self.new_state();self.global_rows[1]["invalid_position_count"]=1;self.flush()
        run=load_run(self.run)
        self.assertEqual(run["maps"]["initialRuntime"][1],[None,None])
        self.assertIsNone(run["evacuation"]["efficiency"])

    def test_zero_duration_has_no_efficiency(self):
        state=self.new_state();state["strategy_applied_time_seconds"]=2.;state["strategy_density"]={"snapshot_id":"s2","valid":True}
        self.write_json("evacuation_state.json",state);run=load_run(self.run)
        self.assertEqual(run["evacuation"]["duration"],0)
        self.assertIsNone(run["evacuation"]["efficiency"])
        self.assertEqual(run["metrics"]["evacuation-efficiency"]["status"],"zero_duration")

    def test_progress_is_matched_to_committed_snapshots(self):
        self.new_state()
        self.write_jsonl("evacuation_progress.jsonl",[
            {"snapshot_id":"s1","last_time_seconds":1.,"progress":{"remaining_person_count":8}},
            {"snapshot_id":"ghost","last_time_seconds":99,"progress":{"remaining_person_count":999}},
        ])
        run=load_run(self.run)
        self.assertEqual(run["evacuation"]["progress_series"],[None,{"remaining_person_count":8},None])

    def test_html_embeds_names_as_data_not_scripts(self):
        manifest=json.loads((self.run/"manifest.json").read_text());manifest["scenario"]["active_hotspot_name"]='</script><script>window.INJECTED=true</script>'
        self.write_json("manifest.json",manifest)
        output=self.root/"viewer"
        data=build_viewer(self.root/"runs",output)
        html=(output/"index.html").read_text()
        self.assertNotIn(manifest["scenario"]["active_hotspot_name"],html)
        self.assertIn("\\u003c/script",html)
        self.assertEqual(json.loads((output/"data.json").read_text()),data)
        self.assertNotIn("__VIEWER_DATA__",html)

    def test_output_cannot_overwrite_recording_directory_or_traverse(self):
        for output in [self.root,self.root/"runs",self.run]:
            with self.assertRaises(ValueError):build_viewer(self.root/"runs",output)
        with self.assertRaises(ValueError):build_viewer(self.root/"runs",self.root/"viewer",["../outside"])

    def test_missing_run_is_reported_and_does_not_hide_good_run(self):
        data=build_viewer(self.root/"runs",self.root/"viewer",["run-missing","run-test"])
        self.assertEqual(len(data["runs"]),1)
        self.assertEqual(data["skipped"][0]["run_id"],"run-missing")

    def test_nonfinite_numbers_never_become_json_nan(self):
        self.global_rows[0]["avg_speed_mps"]="nan";self.flush()
        self.assertIsNone(load_run(self.run)["samples"][0]["avg_speed_mps"])
        self.assertIsNone(number("inf"));self.assertIsNone(number(""));self.assertEqual(number("0"),0)

    def test_b3_weights_use_clipped_cell_area(self):
        self.write_jsonl("commands.jsonl",[{"result":{"action":"start","status":"applied","applied_at":.5}}])
        geometry=json.loads((self.run/"observation_geometry.json").read_text())
        geometry["cells"][0]["area_m2"]=50;geometry["cells"][1]["area_m2"]=150
        self.write_json("observation_geometry.json",geometry)
        run=load_run(self.run)
        self.assertAlmostEqual(run["density_difference_means"][1][0],.025)

    def test_spatial_sampling_preserves_endpoints_and_phase_baselines(self):
        indices=_sample_indices(100,10,[23,58,77])
        self.assertTrue({0,99,23,58,77}.issubset(indices))
        self.assertLessEqual(len(indices),10)
        self.assertEqual(indices,sorted(set(indices)))

    def test_run_id_cannot_change_png_export_path(self):
        manifest=json.loads((self.run/"manifest.json").read_text());manifest["run_id"]="../../outside"
        self.write_json("manifest.json",manifest)
        run=load_run(self.run)
        self.assertEqual(run["id"],"run-test")
        self.assertTrue(any("run_id" in q["message"] for q in run["quality"]))

    def test_current_collector_and_recorder_files_are_read_without_sumo(self):
        from crowdsim.core.population_manager import PopulationLedger
        from crowdsim.domain.crowdsim_models import AgentState
        from crowdsim.infrastructure.observation_metrics import ObservationCollector
        from crowdsim.infrastructure.observation_recorder import ObservationRecorder
        from tests.test_observation_metrics import SQUARE, person, snapshot

        # Fresh files emitted by production code, not hand-written v2 fixtures.
        for name in ["observation_samples.jsonl","state_transitions.jsonl","observation_global.csv",
                     "observation_cells.csv","observation_boundaries.csv"]:
            (self.run/name).unlink()
        collector=ObservationCollector(SQUARE,[m[1] for m in METRICS])
        ledger=PopulationLedger(planned_ids={"a","b"},departed_ids={"a","b"},active_ids={"a","b"})
        recorder=ObservationRecorder(self.run,collector)

        def record(time,people,arrived=()):
            ledger.active_ids={p.person_id for p in people};ledger.arrived_ids.update(arrived)
            step=snapshot(time,*people,arrived=arrived)
            sample=collector.measure(step,{key:AgentState(key) for key in step.persons},{},f"run-test:{time}",ledger=ledger)
            recorder.record(sample)

        record(.5,[person("a"),person("b",30,10)])
        collector.evacuation.start_event(.5,ledger);recorder.sync_lifecycle()
        record(1.5,[person("b",30,10)],arrived=("a",))
        collector.evacuation.policy_applied({"name":"police_guidance","applied_at":1.5,"physical_change":False},{"request_id":"p"})
        recorder.sync_lifecycle()
        record(2.5,[],arrived=("b",));recorder.finalize("complete","normal")
        run=load_run(self.run)
        self.assertEqual(run["quality"],[])
        self.assertEqual(run["evacuation"]["duration"],1.)
        self.assertAlmostEqual(run["evacuation"]["efficiency"],1/1600)
        self.assertEqual(run["evacuation"]["progress_series"][-1]["remaining_person_count"],0)
        self.assertEqual(run["metrics"]["absolute-evacuation-density"]["status"],"complete")


if __name__ == "__main__":
    unittest.main()
