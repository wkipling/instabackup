// Print image base and functions whose entry is within [lo,hi].
// Usage: -postScript ListFuncsNear.java <loHex> <hiHex>
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;

public class ListFuncsNear extends GhidraScript {
    @Override
    public void run() throws Exception {
        long lo = Long.parseUnsignedLong(getScriptArgs()[0].replace("0x", ""), 16);
        long hi = Long.parseUnsignedLong(getScriptArgs()[1].replace("0x", ""), 16);
        println("IMAGE_BASE=" + currentProgram.getImageBase());
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        int n = 0;
        while (it.hasNext()) {
            Function f = it.next();
            long a = f.getEntryPoint().getOffset();
            if (a >= lo && a <= hi) {
                println(String.format("%08x %s size=%d", a, f.getName(), f.getBody().getNumAddresses()));
                n++;
            }
        }
        println("COUNT=" + n);
    }
}
