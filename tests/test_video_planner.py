# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from project_manager import Project, ImageClip
from timeline_planner import plan_timeline, PlanningError, edit_boundary

class PlannerTests(unittest.TestCase):
    def project(self, count=5):
        return Project(audio_duration=40, images=[ImageClip(str(i)) for i in range(count)],
            cues=[dict(start=a,end=b,text=t) for a,b,t in [(0,7,'第一句。'),(8,16,'Second!'),(16,23,'第三句？'),(24,32,'第四句。'),(32,40,'结束。')]])

    def test_forty_seconds(self):
        p=self.project(); plan_timeline(p)
        self.assertEqual([c.end for c in p.images], [7,16,23,32,40])
        self.assertEqual(sum(c.end-c.start for c in p.images),40)

    def test_incomplete_cues_not_boundaries(self):
        p=self.project(); p.cues[0]['text']='未完，'
        with self.assertRaises(PlanningError): plan_timeline(p)

    def test_too_many(self):
        with self.assertRaises(PlanningError): plan_timeline(self.project(6))

    def test_one_image(self):
        p=self.project(1); p.planning['maximum']=60; plan_timeline(p)
        self.assertEqual((p.images[0].start,p.images[0].end),(0,40))

    def test_lock_and_manual_snap(self):
        p=self.project(); plan_timeline(p)
        p.images[0].locked=True
        plan_timeline(p); self.assertEqual(p.images[0].end,7)
        self.assertFalse(edit_boundary(p,0,8,snap=True))
        self.assertTrue(edit_boundary(p,0,8))
        self.assertEqual(p.images[1].start,8)

    def test_no_audio_cut(self):
        p=self.project(); p.planning['target']=30
        with self.assertRaises(PlanningError): plan_timeline(p)
