#!/usr/bin/python
# TODO: move results into directories per config?
# solutions/YYYYMMDD/HHMMSS_IP_XXX.tar.gz
# ->
# processed/XX_Config/YYYYMMDD/HHMMSS_IP_XXX.tar.gz
# TODO: emit config information for cliens for threshold of kmpp to send to server.
# TODO: emit web directory of best-so-far results.
# TODO: extract kmpp_spread.svg from these.

import cgi
import logging
import os
import random
import re
import sys
import sqlite3
import string
import subprocess
import tarfile
import time
import traceback

import resultspage
import runallstates
import states

legpath_ = os.path.join(os.path.dirname(__file__), 'legislatures.csv')


def scandir(path):
	"""Yield (fpath, innerpath) of .tar.gz submissions."""
	for root, dirnames, filenames in os.walk(path):
		for fname in filenames:
			if fname.endswith('.tar.gz'):
				fpath = os.path.join(root, fname)
				assert fpath.startswith(path)
				innerpath = fpath[len(path):]
				#logging.debug('found %s', innerpath)
				yield (fpath, innerpath)


def elementAfter(haystack, needle):
	"""For some sequence haystack [a, needle, b], return b."""
	isNext = False
	for x in haystack:
		if isNext:
			return x
		if x == needle:
			isNext = True
	return None


def extractSome(fpath, names):
	"""From .tar.gz at fpath, get members in list names.
	Return {name; value}."""
	out = {}
	tf = tarfile.open(fpath, 'r:gz')
	for info in tf:
		if info.name in names:
			out[info.name] = tf.extractfile(info).read()
	return out


def atomicLink(src, dest):
	assert dest[-1] != os.sep
	tdest = dest + str(random.randint(100000,999999))
	os.link(src, tdest)
	os.rename(tdest, dest)


# Example analyze output:
# generation 0: 21.679798418 Km/person
# population avg=634910 std=1707.11778
# max=638656 (dist# 10)  min=632557 (dist# 7)  median=634306 (dist# 6)

kmppRe = re.compile(r'([0-9.]+)\s+Km/person')
maxMinRe = re.compile(r'max=([0-9]+).*min=([0-9]+)')


def loadDatadirConfigurations(configs, datadir, statearglist=None, configPathFilter=None):
	"""Store to configs[config name]."""
	for xx in os.listdir(datadir):
		if not os.path.isdir(os.path.join(datadir, xx)):
			logging.debug('data/"%s" not a dir', xx)
			continue
		stu = xx.upper()
		if statearglist and stu not in statearglist:
			#logging.debug('"%s" not in state arg list', stu)
			continue
		configdir = os.path.join(datadir, stu, 'config')
		if not os.path.isdir(configdir):
			logging.debug('no %s/config', xx)
			continue
		for variant in os.listdir(configdir):
			if runallstates.ignoreFile(variant):
				logging.debug('ignore file %s/config/"%s"', xx, variant)
				continue
			cpath = os.path.join(datadir, xx, 'config', variant)
			if configPathFilter and (not configPathFilter(cpath)):
				logging.debug('filter out "%s"', cpath)
				continue
			cname = stu + '_' + variant
			configs[cname] = runallstates.configuration(
				name=cname,
				datadir=os.path.join(datadir, xx),
				config=cpath,
				dataroot=datadir)
			logging.debug('set config "%s"', cname)


class SubmissionAnalyzer(object):
	def __init__(self, options, dbpath=None):
		self.options = options
		# map from STU/config-name to configuration objects
		self.config = {}
		self.dbpath = dbpath
		# sqlite connection
		self.db = None
		self.stderr = sys.stderr
		self.stdout = sys.stdout
		if self.dbpath:
			self.opendb(self.dbpath)
		self.pageTemplate = None
	
	def getPageTemplate(self, rootdir=None):
		if self.pageTemplate is None:
			if rootdir is None:
				rootdir = os.path.dirname(os.path.abspath(__file__))
			f = open(os.path.join(rootdir, 'new_st_index_pyt.html'), 'r')
			self.pageTemplate = string.Template(f.read())
			f.close()
		return self.pageTemplate
	
	def loadDatadir(self, path=None):
		if path is None:
			path = self.options.datadir
		loadDatadirConfigurations(self.config, path)
	
	def opendb(self, path):
		self.db = sqlite3.connect(path)
		c = self.db.cursor()
		# TODO?: make this less sqlite3 specific sql
		c.execute('CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, vars TEXT, unixtime INTEGER, kmpp REAL, spread INTEGER, path TEXT, config TEXT)')
		c.execute('CREATE INDEX IF NOT EXISTS submissions_path ON submissions (path)')
		c.execute('CREATE INDEX IF NOT EXISTS submissions_config ON submissions (config)')
		c.execute('CREATE TABLE IF NOT EXISTS vars (name TEXT PRIMARY KEY, value TEXT)')
		c.close()
		self.db.commit()
	
	def lookupByPath(self, path):
		"""Return db value for path."""
		c = self.db.cursor()
		c.execute('SELECT * FROM submissions WHERE path == ?', (path,))
		out = c.fetchone()
		c.close()
		return out
	
	def measureSolution(self, solraw, configname):
		"""For file-like object of solution and config name, return (kmpp, spread)."""
		#./analyze -B data/MA/ma.pb -d 10 --loadSolution - < rundir/MA_Congress/link1/bestKmpp.dsz
		config = self.config.get(configname)
		if not config:
			logging.warn('config %s not loaded. cannot analyze', configname)
			return None
		datapb = config.args['-P']
		districtNum = config.args['-d']
		cmd = [os.path.join(self.options.bindir, 'analyze'),
			'-P', datapb,
			'-d', districtNum,
			'--loadSolution', '-']
		logging.debug('run %r', cmd)
		p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, shell=False)
		p.stdin.write(solraw)
		p.stdin.close()
		retcode = p.wait()
		if retcode != 0:
			self.stderr.write('error %d running "%s"\n' % (retcode, ' '.join(cmd)))
			return None
		raw = p.stdout.read()
		m = kmppRe.search(raw)
		if not m:
			self.stderr.write('failed to find kmpp in analyze output:\n%s\n' % raw)
			return None
		kmpp = float(m.group(1))
		m = maxMinRe.search(raw)
		if not m:
			self.stderr.write('failed to find max/min in analyze output:\n%s\n' % raw)
			return None
		max = int(m.group(1))
		min = int(m.group(2))
		spread = max - min
		return (kmpp, spread)
	
	def setFromPath(self, fpath, innerpath):
		"""Return True if db was written."""
		tf_mtime = int(os.path.getmtime(fpath))
		tfparts = extractSome(fpath, ('vars', 'solution'))
		if not 'vars' in tfparts:
			logging.warn('no "vars" in "%s"', fpath)
			return False
		vars = cgi.parse_qs(tfparts['vars'])
		config = None
		if 'config' in vars:
			config = vars['config'][0]
		if (not config) and ('localpath' in vars):
			remotepath = vars['path'][0]
			logging.debug('remotepath=%s', remotepath)
			for stu in self.config.iterkeys():
				if stu in remotepath:
					config = stu
					break
		if not config:
			logging.warn('no config for "%s"', fpath)
			return False
		kmppSpread = self.measureSolution(tfparts['solution'], config)
		if kmppSpread is None:
			logging.warn('failed to analyze solution in "%s"', fpath)
			return False
		logging.debug(
			'%s %d kmpp=%f spread=%f from %s',
			config, tf_mtime, kmppSpread[0], kmppSpread[1], innerpath)
		c = self.db.cursor()
		c.execute('INSERT INTO submissions (vars, unixtime, kmpp, spread, path, config) VALUES ( ?, ?, ?, ?, ?, ? )',
			(tfparts['vars'], tf_mtime, kmppSpread[0], kmppSpread[1], innerpath, config))
		return True
	
	def updatedb(self, path):
		"""Update db for solutions under path."""
		if not self.db:
			raise Exception('no db opened')
		setAny = False
		for (fpath, innerpath) in scandir(path):
			x = self.lookupByPath(innerpath)
			if x:
				#logging.debug('already have %s', innerpath)
				continue
			try:
				ok = self.setFromPath(fpath, innerpath)
				setAny = setAny or ok
				logging.info('added %s', innerpath)
			except Exception, e:
				traceback.print_exc()
				logging.warn('failed to process "%s": %r', fpath, e)
				if not self.options.keepgoing:
					break
		if setAny:
			self.db.commit()
	
	def getConfigCounts(self):
		"""For all configurations, return dict mapping config name to a dict {'count': number of solutions reported} for it.
		It's probably handy to extend that dict with getBestSolutionInfo below.
		"""
		c = self.db.cursor()
		rows = c.execute('SELECT config, count(*) FROM submissions GROUP BY config')
		configs = {}
		for config, count in rows:
			configs[config] = {'count': count}
		return configs
	
	def getBestSolutionInfo(self, cname, data):
		"""Set fields in dict 'data' for the best solution to configuration 'cname'."""
		c = self.db.cursor()
		rows = c.execute('SELECT kmpp, spread, id, path FROM submissions WHERE config = ? ORDER BY kmpp DESC LIMIT 1', (cname,))
		rowlist = list(rows)
		assert len(rowlist) == 1
		row = rowlist[0]
		data['kmpp'] = row[0]
		data['spread'] = row[1]
		data['id'] = row[2]
		data['path'] = row[3]
	
	def getBestConfigs(self):
		configs = self.getConfigCounts()
		for cname, data in configs.iteritems():
			self.getBestSolutionInfo(cname, data)
		return configs
	
	def writeConfigOverride(self, outpath):
		out = open(outpath, 'w')
		bestconfigs = self.getBestConfigs()
		for cname, config in self.config.iteritems():
			if cname not in bestconfigs:
				out.write('%s:sendAnything\n' % (cname,))
			else:
				out.write('%s:sendAnything: False\n' % (cname,))
			# TODO: tweak weight/kmppSendTheshold/spreadSendTheshold automatically
			#out.write('%s:disabled\n')
		mpath = outpath + '_manual'
		if os.path.exists(mpath):
			mf = open(mpath, 'r')
			for line in mf:
				out.write(line)
		out.close()
	
	def writeHtml(self, outpath, configs=None):
		if configs is None:
			configs = self.getBestConfigs()
		clist = configs.keys()
		clist.sort()
		out = open(outpath, 'w')
		out.write("""<!doctype html>
<html><head><title>solution report</title><link rel="stylesheet" href="report.css" /></head><body><h1>solution report</h1><p class="gentime">Generated %s</p>
""" % (time.ctime(),))
		out.write('<table><tr><th>config name</th><th>num<br>solutions<br>reported</th><th>best kmpp</th><th>spread</th><th>id</th><th>path</th></tr>\n')
		for cname in clist:
			data = configs[cname]
			out.write('<tr><td>%s</td><td>%d</td><td>%f</td><td>%d</td><td>%d</td><td>%s</td></tr>\n' % (cname, data['count'], data['kmpp'], data['spread'], data['id'], data['path']))
		out.write('</table>\n')
		out.write('</html></body>\n')
		out.close()
	
	def doDrend(self, cname, data, solutionDszRaw, pngpath):
		args = dict(self.config[cname].drendargs)
		args.update({'--loadSolution': '-', '--pngout': pngpath})
		cmd = [os.path.join(self.options.bindir, 'drend')] + runallstates.dictToArgList(args)
		logging.debug('run %r', cmd)
		p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, shell=False)
		p.stdin.write(solutionDszRaw)
		p.stdin.close()
		retcode = p.wait()
		if retcode != 0:
			self.stderr.write('error %d running "%s"\n' % (retcode, ' '.join(cmd)))
			return None
	
	def statenav(self, current, configs=None):
		statevars = {}
		if configs:
			citer = configs.iterkeys()
		else:
			citer = self.config.iterkeys()
		for cname in citer:
			(st, variation) = cname.split('_')
			if st not in statevars:
				statevars[st] = [variation]
			else:
				statevars[st].append(variation)
		outl = []
		for name, stu, house in states.states:
			if stu not in statevars:
				#logging.warn('%s not in current results', stu)
				continue
			variations = statevars[stu]
			if 'Congress' in variations:
				variations.remove('Congress')
				variations.sort()
				variations.insert(0, 'Congress')
			vlist = []
			isCurrent = False
			for v in variations:
				stu_v = stu + '_' + v
				if stu_v == current:
					isCurrent = True
					vlist.append('<b>%s</b>' % (v,))
				else:
					vlist.append('<a href="%s">%s</a>' % (self.options.rooturl + '/' + stu_v + '/', v))
			if isCurrent:
				dclazz = 'slgC'
			else:
				dclazz = 'slg'
			outl.append('<div class="%s">%s %s</div>' % (dclazz, name, ' '.join(vlist)))
		return '<div class="snl">' + ''.join(outl) + '</div>'
	
	def buildBestSoFarDirs(self, configs=None):
		"""$outdir/$XX_yyy/$id/{index.html,ba_500.png,ba.png,map.png,map500.png}
		With hard links from $XX_yyy/* to $XX_yyy/$id/* for the current best."""
		outdir = self.options.outdir
		if not os.path.isdir(outdir):
			os.makedirs(outdir)
		if configs is None:
			configs = self.getBestConfigs()
		for cname, data in configs.iteritems():
			sdir = os.path.join(outdir, cname, str(data['id']))
			if not os.path.isdir(sdir):
				os.makedirs(sdir)
			ihpath = os.path.join(sdir, 'index.html')
			if os.path.exists(ihpath):
				# already made, no need to re-write it
				continue
			tpath = data['path']
			if tpath[0] == os.sep:
				tpath = tpath[len(os.sep):]
			tpath = os.path.join(self.options.soldir, tpath)
			tfparts = extractSome(tpath, ('solution', 'statsum'))
			mappath = os.path.join(sdir, 'map.png')
			if not os.path.exists(mappath):
				self.doDrend(cname, data, tfparts['solution'], mappath)
			# TODO: run `measurerace` to get demographic analysis
			# 500x500 smallish size version
			map500path = os.path.join(sdir, 'map500.png')
			if not os.path.exists(map500path):
				subprocess.call(['convert', mappath, '-resize', '500x500', map500path])
			(kmpp, spread, std) = resultspage.parse_statsum(tfparts['statsum'])
			st_template = self.getPageTemplate()
			out = open(ihpath, 'w')
			# TODO: permalink
			permalink = self.options.rooturl + '/' + cname + '/' + str(data['id']) + '/'
			out.write(st_template.substitute(
				statename=cname,
				statenav=self.statenav(cname, configs),
				ba_large='map.png',
				ba_small='map500.png',
				avgpop='',
				current_kmpp='',
				current_spread='',
				current_std='',
				my_kmpp=str(kmpp),
				my_spread=str(spread),
				my_std=str(std),
				extra='',
				racedata='',
				rooturl=self.options.rooturl,
			))
			out.close()
			for x in ('map.png', 'map500.png', 'index.html'):
				atomicLink(os.path.join(sdir, x), os.path.join(outdir, cname, x))
		result_index_html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'result_index_pyt.html')
		f = open(result_index_html_path, 'r')
		result_index_html_template = string.Template(f.read())
		f.close()
		index_html = open(os.path.join(outdir, 'index.html'), 'w')
		index_html.write(result_index_html_template.substitute(
			statenav=self.statenav(None, configs),
			rooturl=self.options.rooturl,
		))
		index_html.close()


def main():
	import optparse
	argp = optparse.OptionParser()
	default_bindir = runallstates.getDefaultBindir()
	argp.add_option('-d', '--data', '--datadir', dest='datadir', default=runallstates.getDefaultDatadir(default_bindir))
	argp.add_option('--bindir', '--bin', dest='bindir', default=default_bindir)
	argp.add_option('--keep-going', '-k', dest='keepgoing', default=False, action='store_true', help='like make, keep going after failures')
	argp.add_option('--soldir', '--solutions', dest='soldir', default='.', help='directory to scan for solutions')
	argp.add_option('--do-update', dest='doupdate', default=True)
	argp.add_option('--no-update', dest='doupdate', action='store_false')
	argp.add_option('--report', dest='report', default='report.html', help='filename to write html report to.')
	argp.add_option('--outdir', dest='outdir', default='report', help='directory to write html best-so-far displays to.')
	argp.add_option('--configoverride', dest='configoverride', default=None, help='where to write configoverride file')
	argp.add_option('--verbose', '-v', dest='verbose', action='store_true', default=False)
	argp.add_option('--rooturl', dest='rooturl', default='file://' + os.path.abspath('.'))
	(options, args) = argp.parse_args()
	if options.verbose:
		logging.getLogger().setLevel(logging.DEBUG)
	x = SubmissionAnalyzer(options, dbpath='.status.sqlite3')
	logging.debug('loading datadir')
	x.loadDatadir(options.datadir)
	logging.debug('done loading datadir')
	if options.soldir and options.doupdate:
		x.updatedb(options.soldir)
	configs = None
	if options.report or options.outdir:
		configs = x.getBestConfigs()
	if options.configoverride:
		x.writeConfigOverride(options.configoverride)
	if options.report:
		x.writeHtml(options.report, configs)
	if options.outdir:
		x.buildBestSoFarDirs(configs)


if __name__ == '__main__':
	main()