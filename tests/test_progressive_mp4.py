"""Range 完整资源资格与 MSE 归属分别验证；包含预加载和歧义反例。"""
from copy import deepcopy
from dataclasses import replace
import unittest

from core.discovery_models import NetworkObservation
from core.media_discovery import MediaDiscoveryService
from core.media_guard import MediaSnapshot
from core.manifests import parse_manifest
from core.progressive_mp4 import range_proof, full_resource_context
from core.request_context import RequestContext
from tests.test_media_details import atom, track


def movie(duration=19, width=576, height=1024, fragmented=False):
    moov = track(width=width, height=height, timescale=1000, ticks=int(duration*1000), delta=33)
    moov += track(b'soun', width=0, height=0, timescale=1000, ticks=int(duration*1000))
    if fragmented:
        moov += atom(b'mvex')
    return atom(b'ftyp', b'isom'+bytes(4)+b'iso6')+atom(b'moov', moov)+atom(b'mdat', b'x'*64000)


def observation(data, url='https://cdn.example/movie', request='first'):
    prefix=data[:12000]
    context=RequestContext(url, headers={'range':'bytes=0-11999','if-range':'"old"',
        'cookie':'private-cookie','referer':'https://page.example/watch?modal_id=111'}, inherit_range=True)
    return NetworkObservation(request, context, 206, 'video/mp4', prefix, frame_id='main',
        content_length=len(prefix), body_complete=True,
        response_headers={'Content-Range':f'bytes 0-{len(prefix)-1}/{len(data)}','Content-Length':str(len(prefix))})


def response(obs, data, **changes):
    values=dict(context=full_resource_context(obs.context), data=data[:16384], content_type='video/mp4',
        info=parse_manifest(data[:16384], obs.context.final_url, 'video/mp4'), http_status=200,
        response_headers={'Content-Length':str(len(data)),'Content-Type':'video/mp4'})
    values.update(changes)
    return MediaSnapshot(**values)


def player(duration=19.16, width=576, height=1024, visible=True, name='main'):
    return {'current_src':'blob:https://page.example/'+name, 'src':'blob:https://page.example/'+name,
        'tag':'VIDEO','element_id':name,'duration':duration,'width':width,'height':height,
        'visible':visible,'is_main':visible,'paused':not visible,'ready_state':4,'area':500000 if visible else 0}


class ProgressiveMp4Tests(unittest.TestCase):
    def setUp(self):
        self.service=MediaDiscoveryService()
        self.data=movie()
        self.obs=observation(self.data)

    def add(self, obs, data, verified=True):
        self.service.consume_network(obs)
        c=next(c for c in self.service.candidates.values() if c.url==obs.context.url)
        if verified:
            root=response(obs,data)
            self.assertTrue(self.service.apply_progressive_probe(c.id,root))
            self.service.apply_probe(c.id,{**root.info.to_dict(),'http_status':200})
        return c

    def dom(self, videos):
        self.service.consume_dom({'page_url':'https://page.example/watch?modal_id=111',
            'frame_id':'main','timestamp':1000,'videos':videos})

    def test_partial_stays_unavailable_until_independent_full_resource_check(self):
        c=self.add(self.obs,self.data,False)
        self.assertEqual(c.media_type,'Segment')
        self.assertFalse(c.downloadable)
        self.dom([player()])
        self.assertFalse(c.metadata['confidence_evidence'].get('current_player'))
        root=response(self.obs,self.data)
        self.assertTrue(self.service.apply_progressive_probe(c.id,root))
        self.service.apply_probe(c.id,{**root.info.to_dict(),'http_status':200})
        self.assertTrue(c.downloadable)
        self.assertEqual(c.media_type,'Direct')
        self.assertNotIn('range',c.request_context.headers)
        self.assertNotIn('if-range',c.request_context.headers)
        self.assertEqual(c.request_context.cookies,'private-cookie')
        self.assertTrue(c.metadata['confidence_evidence']['current_player'])

    def test_init_fragment_incomplete_moov_and_nonzero_range_never_get_proof(self):
        self.assertIsNone(range_proof(observation(movie(fragmented=True))))
        init=atom(b'ftyp',b'isom'+bytes(4))+atom(b'moov',track())
        self.assertIsNone(range_proof(observation(init)))
        self.assertIsNone(range_proof(replace(self.obs,body=self.obs.body[:40],body_complete=False)))
        self.assertIsNone(range_proof(replace(self.obs,response_headers={
            'Content-Range':f'bytes 100-12099/{len(self.data)}','Content-Length':'12000'})))

    def test_wrong_total_prefix_partial_response_or_drm_cannot_promote(self):
        c=self.add(self.obs,self.data,False)
        root=response(self.obs,self.data)
        variants=[replace(root,response_headers={'Content-Length':'999'}),
            replace(root,data=b'wrong-resource'+root.data),
            replace(root,http_status=206,response_headers={'Content-Length':'12000',
                'Content-Range':f'bytes 0-11999/{len(self.data)}'}),
            replace(root,info=replace(root.info,drm=True))]
        for invalid in variants:
            self.assertFalse(self.service.apply_progressive_probe(c.id,invalid))
            self.assertFalse(c.downloadable)

    def test_hidden_short_preload_cannot_overwrite_visible_main(self):
        main=self.add(self.obs,self.data)
        short_data=movie(2.6)
        short=self.add(observation(short_data,'https://cdn.example/preload'),short_data)
        self.dom([player(),player(2.6,visible=False,name='preload')])
        self.assertTrue(main.metadata['confidence_evidence']['current_player'])
        self.assertEqual(main.metadata['dom_video_id'],'main')
        self.assertAlmostEqual(main.duration,19.16)
        self.assertFalse(short.metadata['confidence_evidence']['current_player'])
        self.assertLess(short.confidence,main.confidence)

    def test_same_metadata_candidates_or_players_keep_ambiguity(self):
        main=self.add(self.obs,self.data)
        duplicate=self.add(observation(self.data,'https://cdn.example/another'),self.data)
        self.dom([player()])
        self.assertFalse(main.metadata['confidence_evidence'].get('current_player'))
        self.assertFalse(duplicate.metadata['confidence_evidence'].get('current_player'))
        self.service=MediaDiscoveryService()
        main=self.add(self.obs,self.data)
        self.dom([player(),player(19.16,visible=False,name='same-shape-preload')])
        self.assertFalse(main.metadata['confidence_evidence'].get('current_player'))

    def test_unknown_segment_competitor_cannot_manufacture_unique_match(self):
        main=self.add(self.obs,self.data)
        unknown=replace(self.obs,request_id='other',context=RequestContext('https://cdn.example/unknown'),body=None)
        self.service.consume_network(unknown)
        self.dom([player()])
        self.assertFalse(main.metadata['confidence_evidence'].get('current_player'))

    def test_direct_audio_is_not_a_video_player(self):
        obs=replace(self.obs,status=200,body=None,content_type='audio/mpeg',
                    context=RequestContext('https://cdn.example/music'))
        self.service.consume_network(obs)
        self.dom([player(),player(2.6,visible=False,name='preload')])
        c=next(iter(self.service.candidates.values()))
        self.assertFalse(c.metadata['confidence_evidence'].get('current_player'))
        self.assertIsNone(c.duration)

    def test_late_range_keeps_verified_direct_and_clean_download_context(self):
        c=self.add(self.obs,self.data)
        self.service.consume_network(replace(self.obs,request_id='later',body=None,
            context=replace(self.obs.context,headers={**self.obs.context.headers,'range':'bytes=12000-'})))
        self.assertEqual(c.media_type,'Direct')
        self.assertTrue(c.downloadable)
        self.assertNotIn('range',c.request_context.headers)

    def test_changed_resource_version_rejects_inflight_old_probe(self):
        obs=replace(self.obs,response_headers={**self.obs.response_headers,'ETag':'"v1"'})
        c=self.add(obs,self.data,False)
        old=response(obs,self.data,response_headers={'Content-Length':str(len(self.data)),'ETag':'"v1"'})
        self.service.consume_network(replace(obs,request_id='new-version',body=None,
            response_headers={'Content-Length':'12000','ETag':'"v2"',
                'Content-Range':f'bytes 0-11999/{len(self.data)+1000}'}))
        self.assertNotIn('progressive_mp4',c.metadata)
        self.assertFalse(self.service.apply_progressive_probe(c.id,old))
        self.assertFalse(c.downloadable)

    def test_manifest_competitor_or_unknown_blob_preserves_ambiguity(self):
        c=self.add(self.obs,self.data)
        manifest=NetworkObservation('manifest',RequestContext('https://cdn.example/index'),200,
            'application/vnd.apple.mpegurl',b'#EXTM3U\n#EXTINF:19,\na.ts\n#EXT-X-ENDLIST\n',frame_id='main',body_complete=True)
        self.service.consume_network(manifest)
        self.dom([player()])
        self.assertFalse(c.metadata['confidence_evidence'].get('current_player'))

    def test_proven_different_size_preload_tracks_do_not_hide_current_progressive_video(self):
        c=self.add(self.obs,self.data)
        for name,payload in (('video-init',track(width=576,height=768,ticks=0)),
                             ('audio-init',track(b'soun',width=0,height=0,ticks=0))):
            init=atom(b'ftyp',b'isom'+bytes(4)+b'iso6')+atom(b'moov',payload+atom(b'mvex'))
            obs=observation(init,'https://cdn.example/'+name)
            obs=replace(obs,response_headers={'Content-Length':str(len(init)),
                'Content-Range':f'bytes 0-{len(init)-1}/999999'})
            self.service.consume_network(obs)
        self.dom([player(),player(345,width=576,height=768,visible=False,name='preload')])
        self.assertTrue(c.metadata['confidence_evidence'].get('current_player'))
        for other in self.service.candidates.values():
            if other.id!=c.id:self.assertFalse(other.downloadable)
        self.service=MediaDiscoveryService()
        c=self.add(self.obs,self.data)
        self.dom([player(),player(duration=None,width=0,height=0,visible=False,name='unknown')])
        self.assertFalse(c.metadata['confidence_evidence'].get('current_player'))

    def test_fragment_local_duration_does_not_exclude_a_same_size_competitor(self):
        c=self.add(self.obs,self.data)
        init=atom(b'ftyp',b'isom'+bytes(4)+b'iso6')+atom(b'moov',
            track(width=576,height=1024,timescale=1000,ticks=2600)+atom(b'mvex'))
        obs=observation(init,'https://cdn.example/partial-track')
        self.service.consume_network(replace(obs,response_headers={'Content-Length':str(len(init)),
            'Content-Range':f'bytes 0-{len(init)-1}/999999'}))
        self.dom([player()])
        self.assertFalse(c.metadata['confidence_evidence'].get('current_player'))

    def add_init(self,payload,name='init'):
        init=atom(b'ftyp',b'isom'+bytes(4)+b'iso6')+atom(b'moov',payload+atom(b'mvex'))
        obs=observation(init,'https://cdn.example/'+name)
        obs=replace(obs,response_headers={'Content-Length':str(len(init)),
            'Content-Range':f'bytes 0-{len(init)-1}/999999','ETag':'"init-v1"'})
        self.service.consume_network(obs)
        return obs

    def test_empty_or_unknown_tracks_cannot_be_called_audio_only(self):
        for payload in (b'',track(b'soun')+atom(b'trak')):
            self.service=MediaDiscoveryService()
            c=self.add(self.obs,self.data)
            self.add_init(payload)
            self.dom([player()])
            self.assertFalse(c.metadata['confidence_evidence'].get('current_player'))

    def test_multiple_video_tracks_cannot_be_ruled_out_by_first_dimensions(self):
        c=self.add(self.obs,self.data)
        self.add_init(track(width=576,height=768,ticks=0)+track(width=576,height=1024,ticks=0))
        self.dom([player()])
        self.assertFalse(c.metadata['confidence_evidence'].get('current_player'))

    def test_changed_init_version_invalidates_old_negative_identity(self):
        c=self.add(self.obs,self.data)
        init=self.add_init(track(width=576,height=768,ticks=0))
        self.dom([player()])
        self.assertTrue(c.metadata['confidence_evidence'].get('current_player'))
        self.service.consume_network(replace(init,request_id='new-init-version',body=None,
            response_headers={'Content-Length':str(len(init.body)),'ETag':'"init-v2"',
                'Content-Range':f'bytes 0-{len(init.body)-1}/1111111'}))
        self.assertFalse(c.metadata['confidence_evidence'].get('current_player'))


if __name__=='__main__':
    unittest.main()
