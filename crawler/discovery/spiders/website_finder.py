# -*- coding: utf-8 -*-
from __future__ import absolute_import

import random
import datetime
import base64
from collections import defaultdict
from scrapy import log

import scrapy
from scrapy.contrib.linkextractors import LinkExtractor

from discovery.urlutils import (
    add_scheme_if_missing,
    is_external_url,
    get_domain,
)
from discovery.screenshots import save_screenshot
from crawler.discovery.items import WebpageItemLoader


class SplashSpiderBase(scrapy.Spider):
    def __init__(self, screenshot_dir, **kwargs):
        super(SplashSpiderBase, self).__init__(**kwargs)
        self.screenshot_dir = screenshot_dir
        log.msg("Screenshot dir: ", log.INFO)
        log.msg(self.screenshot_dir, log.INFO)

    def _splash_request(self, url):
        screq = scrapy.Request(url, meta={
            'splash': {
                'html': '1' if self.save_html else '0',
                'png': '1',
                'wait': '2.0',
                'width': '640',
                'height': '480',
                'timeout': '60',
                'images' : 0
            },
            'download_timeout': 60,
        })
        return screq

    def _process_splash_response(self, response, ld):
        screenshot_path = save_screenshot(
            screenshot_dir=self.screenshot_dir,
            prefix=get_domain(response.url),
            png=base64.b64decode(response.meta['splash_response']['png']),
        )
        ld.add_value('screenshot_path', screenshot_path)

        if self.save_html:
            ld.add_value('html_rendered', response.meta['splash_response']['html'])


class WebsiteFinderSpider(SplashSpiderBase):
    """
    A spider to find new websites given a comma-separated list of seed URLs.
    To start it from command-line run::

        $ scrapy crawl website_finder -a seed_urls=example.com,example2.co.uk -o data.csv

    To enable HTML storage use 'save_html' argument::

        $ scrapy crawl website_finder -a seed_urls=example.com -a save_html=1 -o data.csv

    To disable Javascript rendering and screenshots pass 'use_splash=0' argument::

        $ scrapy crawl website_finder -a seed_urls=example.com -a use_splash=0 -o data.csv

    (note that data is appended to data.csv, so you might want to remove old file)

    Navigation algorithm is the following:

    1. Visit each of the seed URLs.
    2. Each seed url is crawled to depth ``max_depth_seed``,
       but no more than ``max_internal_links_per_seed`` are followed.
    3. All found external URLs are followed, but if several external links are
       to the same domain then at most ``max_external_links_per_seed_per_domain``
       are followed.
    4. Each external domain is crawled to depth ``max_depth_external``,
       but no more than ``max_external_links_per_domain`` is followed for each
       domain.
    5. If an external website has links to other websites they are also
       followed; ``max_depth_external`` depth limit counts from zero at such
       pages, but limits from ``max_external_links_per_domain`` are kept and
       respected.
    6. There is a global depth limit (settings.DEPTH_LIMIT) which effectively
       limits number of hops.

    """
    name = 'website_finder'
    random_seed = 0
    save_html = None
    use_splash = None

    # FIXME: these limits don't take duplicates filter in account
    max_depth_seed = 2
    max_depth_external = 2
    max_internal_links_per_seed = 100
    max_external_links_per_seed_per_domain = 5
    max_external_links_per_domain = 10

    #screenshot_dir = '/memex-pinterest/ui/static/images/screenshots'

    def __init__(self, seed_urls, save_html=1, use_splash=1, screenshot_dir=None, **kwargs):
        self.save_html = bool(int(save_html))
        self.use_splash = bool(int(use_splash))
        self.random = random.Random(self.random_seed)
        self.start_urls = [add_scheme_if_missing(url) for url in seed_urls.split(',')]
        self.req_count = defaultdict(int)
        super(WebsiteFinderSpider, self).__init__(name=None, screenshot_dir=screenshot_dir, **kwargs)

    def make_requests_from_url(self, url, is_seed=False):
        return self._new_request(url, self.parse, {})

    def parse(self, response):
        if 'referrer_url' in response.meta:
            if is_external_url(response.url, response.meta['referrer_url']):
                # When we follow a link and it redirects to another domain
                # consider it external even if the link url was on-site.
                response.meta['link_depth'] = 0
                return self.parse_external(response)

        return self.parse_seed(response)

    def parse_seed(self, response):
        """
        Parse a webpage from the "seed" website.
        """
        ld = self._load_webpage_item(response, is_seed=True)

        if self.use_splash:
            self._process_splash_response(response, ld)

        yield ld.load_item()

        this_domain = get_domain(response.url)

        for link in self._get_links(response):
            domain = get_domain(link.url)

            if is_external_url(response.url, link.url):
                yield self._offsite_request(
                    response, link,
                    count_key=(this_domain, domain),
                    max_count=self.max_external_links_per_seed_per_domain
                )
            else:
                yield self._onsite_request(
                    response, link,
                    callback=self.parse,
                    max_depth=self.max_depth_seed,
                    count_key=domain,
                    max_count=self.max_internal_links_per_seed,
                )

    def parse_external(self, response):
        """
        Parse a webpage from an external website.
        """
        ld = self._load_webpage_item(response, is_seed=False)

        if self.use_splash:
            self._process_splash_response(response, ld)

        yield ld.load_item()

        for link in self._get_links(response):
            domain = get_domain(link.url)

            if is_external_url(response.url, link.url):
                # total number of hops is limited by settings.DEPTH_LIMIT
                yield self._offsite_request(
                    response, link,
                    count_key=domain,
                    max_count=self.max_external_links_per_domain
                )
            else:
                yield self._onsite_request(
                    response, link,
                    callback=self.parse_external,
                    max_depth=self.max_depth_external,
                    count_key=domain,
                    max_count=self.max_external_links_per_domain
                )

    def _get_links(self, response):
        links = LinkExtractor().extract_links(response)

        # TODO: remove facebook, twitter, google, various stats counters?

        # shuffle links to avoid missing some parts of a page
        # because of per-domain limits
        self.random.shuffle(links)

        return links

    def _offsite_request(self, response, link, count_key, max_count):
        if self.req_count[count_key] > max_count:
            return
        self.req_count[count_key] += 1

        depth = response.meta.get('link_depth', 0)

        if self.req_count[count_key] <= max_count:
            return self._new_request(link.url, self.parse_external, {
                'link': link,
                'link_depth': 0,
                'referrer_depth': depth,
                'referrer_url': response.url,
            })

    def _onsite_request(self, response, link, callback, max_depth, count_key, max_count):
        """ Return an on-site request or None """
        depth = response.meta.get('link_depth', 0)

        if depth >= max_depth:
            return

        if self.req_count[count_key] >= max_count:
            # self.log("Number of internal links on seed domain %s is greater than %s, ignoring %s" % (domain, self.max_internal_links_per_seed, link.url))
            return

        self.req_count[count_key] += 1

        return self._new_request(link.url, callback, {
            'link': link,
            'link_depth': depth + 1,
            'referrer_depth': depth,
            'referrer_url': response.url,
        })

    def _new_request(self, url, callback, meta):
        r = self._splash_request(url) if self.use_splash else scrapy.Request(url)
        r.callback = callback
        r.meta.update(meta)
        return r

    def _load_webpage_item(self, response, is_seed):
        depth = response.meta.get('link_depth', 0)
        ld = WebpageItemLoader(response=response)
        ld.add_value('url', response.url)
        ld.add_value('host', get_domain(response.url))
        ld.add_xpath('title', '//title/text()')
        ld.add_value('depth', depth)
        ld.add_value('total_depth', response.meta.get('depth'))
        ld.add_value('crawled_at', datetime.datetime.utcnow())
        ld.add_value('is_seed', is_seed)

        if self.save_html:
            ld.add_value('html', response.body_as_unicode())

        if 'link' in response.meta:
            link = response.meta['link']
            ld.add_value('link_text', link.text)
            ld.add_value('link_url', link.url)
            ld.add_value('referrer_url', response.meta['referrer_url'])
            ld.add_value('referrer_depth', response.meta['referrer_depth'])
        return ld
