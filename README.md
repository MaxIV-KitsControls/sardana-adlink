# sardana-adlink
Repository for the Sardana plugins related to AdLink.

- The first channel should always be Timer channel (1)
- Add remaining channels as required

### AdlinkAIOneDCtrl
- Returns count.
- Works well for step as well as continuous scans.

### AdlinkAIOneDCtrl
- Multiple HW trigger on each step.
- Can't send multiple Software Triggers in SW Synchronization, so will always be one point 1D.
- `PointsPerStep` for how many points per step. Should correspond to the incoming triggers per step and should be configured before the scan.