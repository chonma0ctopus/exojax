import pytest
import pkg_resources
import pandas as pd
import numpy as np
from exojax.test.data import TESTDATA_CO_EXOMOL_LPF_XS_REF
from exojax.test.data import TESTDATA_CO_HITEMP_LPF_XS_REF
from exojax.test.emulate_mdb import mock_wavenumber_grid
from exojax.test.emulate_mdb import mock_mdb
from exojax.spec.opacalc import OpaDirect
import matplotlib.pyplot as plt
    
testdata = {}
testdata["exomol"] = TESTDATA_CO_EXOMOL_LPF_XS_REF
testdata["hitemp"] = TESTDATA_CO_HITEMP_LPF_XS_REF


@pytest.mark.parametrize("db",["exomol","hitemp"])
def test_xsection(db):
    mdbCO = mock_mdb(db)
    Tfix=1200.0
    Pfix=1.0
    nu_grid, wav, res = mock_wavenumber_grid()
    opa = OpaDirect(mdbCO, nu_grid)
    xsv = opa.xsvector(Tfix, Pfix)
    filename = pkg_resources.resource_filename('exojax', 'data/testdata/'+testdata[db])
    plt.plot(nu_grid,xsv)
    #dat=pd.read_csv(filename,delimiter=",",names=("nus","xsv"))
    #assert np.all(xsv == pytest.approx(dat["xsv"].values))
    
if __name__ == "__main__":
    test_xsection("hitemp")
    #test_xsection("exomol")
    plt.show()
    