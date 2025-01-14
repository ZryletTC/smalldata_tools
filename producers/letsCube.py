#!/usr/bin/env python

import sys
import os
import time
from datetime import datetime
import numpy as np
import argparse
import socket
import logging 
import os
import requests
from requests.auth import HTTPBasicAuth
from mpi4py import MPI
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

fpath=os.path.dirname(os.path.abspath(__file__))
fpathup = '/'.join(fpath.split('/')[:-1])
sys.path.append(fpathup)
print(fpathup)
import smalldata_tools.cube.cube_mpi_fun as mpi_fun

# exp = 'xpplv9818'
# run = 127

# exp = 'xpplw8919'
# run = 63

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

parser = argparse.ArgumentParser()
parser.add_argument('--run', help='run', type=str, default=os.environ.get('RUN_NUM', ''))
parser.add_argument('--experiment', help='experiment name', type=str, default=os.environ.get('EXPERIMENT', ''))
parser.add_argument("--indirectory", help="directory w/ smallData file if not default")
parser.add_argument("--outdirectory", help="directory w/ smallData for cube if not same as smallData", default='')
parser.add_argument("--nevents", help="number of events/bin", default=-1)
parser.add_argument("--postRuntable", help="postTrigger for seconday jobs", action='store_true')
parser.add_argument('--url', default="https://pswww.slac.stanford.edu/ws-auth/lgbk/")
args = parser.parse_args()
    
exp = args.experiment
run = args.run

if rank==0:
    from smalldata_tools.SmallDataAna import SmallDataAna
    from smalldata_tools.SmallDataAna_psana import SmallDataAna_psana
    import smalldata_tools.cube.cube_rank0_utils as utils
    
    # Constants
    HUTCHES = [
        'AMO',
        'SXR',
        'XPP',
        'XCS',
        'MFX',
        'CXI',
        'MEC'
    ]

    FFB_BASE = '/cds/data/drpsrcf'
    PSDM_BASE = '/reg/d/psdm'
    SD_EXT = '/hdf5/smalldata'

    begin_prod_time = datetime.now().strftime('%m/%d/%Y %H:%M:%S')

    ##### START SCRIPT ########

    # Define hostname
    hostname = socket.gethostname()

    # Parse hutch name from experiment and check it's a valid hutch
    exp = args.experiment
    run = args.run
    logger.debug('Analyzing data for EXP:{0} - RUN:{1}'.format(args.experiment, args.run))

    hutch = exp[:3].upper()
    if hutch not in HUTCHES:
        logger.debug('Could not find {0} in list of available hutches'.format(hutch))
        sys.exit()
    # load config file for hutch
    if hutch=='XPP':
        import cube_config_xpp as config
    elif hutch=='XCS':
        import cube_config_xcs as config

    dirname=''
#     dirname='/cds/data/psdm/xpp/xpplv9818/scratch/ffb/hdf5/smalldata'
#     dirname = '/cds/data/drpsrcf/xpp/xpplw8919/scratch/hdf5/smalldata'
    if args.indirectory:
        dirname = args.indirectory
        if dirname.find('/')<=0:
            dirname+='/'
        
    ana = None
    anaps = SmallDataAna_psana(exp,run,dirname)
    if anaps and anaps.sda is not None and 'fh5' in anaps.sda.__dict__.keys():
        print('create ana module from anaps')
        ana = anaps.sda
    else:
        print('we will now try to open the littleData file directly')
        ana = SmallDataAna(exp,run, dirname, fname)
        if 'fh5' not in ana.__dict__.keys():
            ana = None
    if ana is None:
        print('Non ana instance found. Abort')
        comm.Abort()

    ana.printRunInfo()

    for filt in config.filters:
        ana.addCut(*filt)

    varList = config.varList

    #CHANGE ME
    ####
    # if you are interested in laser-xray delay, please select the delay of choice here!
    ####
    ana.setDelay(use_ttCorr=True, addEnc=False, addLxt=False, reset=True)
    
    cubeName='cube' #initial name
    scanName, scanValues = ana.getScanValues()
    binSteps=[]
    binName=''
#     filterName='filter1'
    if scanName!='':
        if scanName.find('delay')<0:
            scanSteps = np.unique(scanValues)
            scanSteps = np.append(scanSteps, scanSteps[-1]+abs(scanSteps[1]-scanSteps[0]))#catch value at right edge?
            scanSteps = np.append(scanSteps[0]-abs(scanSteps[1]-scanSteps[0]),scanSteps) #catch values at left edge
            binSteps = scanSteps
            cubeName = scanName
            if scanName == 'lxt':    
                print('bin data using ',scanName,' and bins: ',scanSteps)
                binName='delay'
            else:
                print('bin data using ',scanName,' and bins: ',scanSteps)
                binName='scan/%s'%scanName
        else:
            binSteps = config.binBoundaries(run)
            cubeName = 'delay'
            if binSteps is None:
                #assume a fast delay scan here.
                cubeName = 'lasDelay'
                enc = ana.getVar('enc/lasDelay')
                binSteps = np.arange( (int(np.nanmin(enc*10)))/10., 
                                     (int(np.nanmax(enc*10)))/10., 0.1)
            binName = 'delay'
    else:
        cubeName='randomTest'
        binName='ipm2/sum'
        binVar=ana.getVar(binName)
        binSteps=np.percentile(binVar,[0,25,50,75,100])

    print('Bin name: {}, bins: {}'.format(binName, binSteps))

    if int(args.nevents)>0: # not really implemented
        cubeName+='_%sEvents'%args.nevents
    
    for filterName in ana.Sels:
        if filterName!='filter1':
            cubeName = cubeName+'_'+filterName
        ana.addCube(cubeName,binName,binSteps,filterName)
        ana.addToCube(cubeName,varList)

    anaps._broadcast_xtc_dets(cubeName) # send detectors dict to workers. All cubes MUST use the same det list.
    
    for ii,cubeName in enumerate(ana.cubes):
        print('Cubing {}'.format(cubeName))
        comm.bcast('Work!', root=0)
        time.sleep(1) # is this necessary? Just putting it here in case...
        if config.laser:
            #request 'on' events base on input filter (add optical laser filter)
            anaps.makeCubeData(cubeName, onoff=1, nEvtsPerBin=args.nevents, dirname=args.outdirectory)
            comm.bcast('Work!', root=0)
            time.sleep(1) # is this necessary? Just putting it here in case...
            #request 'off' events base on input filter (switch optical laser filter, drop tt
            anaps.makeCubeData(cubeName, onoff=0, nEvtsPerBin=args.nevents, dirname=args.outdirectory)
        else:
            # no laser filters
            anaps.makeCubeData(cube.cubeName, onoff=2, nEvtsPerBin=args.nevents, dirname=args.outdirectory)
    comm.bcast('Go home!', root=0)
        
    if config.save_tiff is True:
        tiffdir='/cds/data/drpsrcf/mec/meclu9418/scratch/run%s'%args.run
        cubeFName = 'Cube_%s_Run%04d_%s'%(args.experiment, int(run), cubeName )
        cubedirectory= args.outdirectory
        if cubedirectory=='':
            cubedirectory = '/cds/data/drpsrcf/mec/meclu9418/scratch/hdf5/smalldata'
        dat = tables.open_file('%s/%s.h5'%(cubedirectory, cubeFName )).root
        detnames = ['Epix10kaQuad2','epix100a_1_IXS','epix100a_2_XRTS']
        for detname in detnames:
            cubedata = np.squeeze(getattr(dat, detname+'_data'))
            if cubedata.ndim==2:
                im = Image.fromarray(cubedata)
                tiff_file = '%s/Run_%s_%s_filter_%s.tiff'%(tiffdir, run, detname, filterName)
                im.save(tiff_file)
                
    if int(os.environ.get('RUN_NUM', '-1')) > 0:
        requests.post(os.environ["JID_UPDATE_COUNTERS"], json=[{"key": "<b>Cube </b>", "value": "Done"}])
    
    # Make histogram summary plots
    tabs = utils.make_report(anaps, config.hist_list, config.filters, config.varList, exp, run)
        
else:
    work = 1
    binWorker = mpi_fun.BinWorker(run, exp)
    while(work):
        time.sleep(1) # is this necessary? Just putting it here in case...
        amIStillWorking = comm.bcast(None, root=0)
        if amIStillWorking=='Go home!':
            work = 0
            logger.debug('Yay, work\'s over!')
        elif amIStillWorking=='Work!':
            logger.debug('Oh no, I\'m getting more work!')
            binWorker.work()
